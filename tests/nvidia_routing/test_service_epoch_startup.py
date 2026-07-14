"""Ordered service epoch startup and failure cleanup."""

from typing import override
from uuid import UUID

import anyio
import pytest

import nvidia_build_lb.service_epoch as epoch_module
from nvidia_build_lb.service_epoch import (
    ServiceEpochCoordinator,
    ServiceEpochDependencies,
    ServiceEpochLockUnavailableError,
)
from nvidia_build_lb.service_epoch_monitor import MonitorEvent
from nvidia_build_lb.service_epoch_types import EpochSqlConnection, PriorEpochCleanup

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_EPOCH = UUID("00000000-0000-4000-8000-000000000401")


class _Connection:
    autocommit: bool = True
    dedicated: bool = True
    driver: str = "psycopg_async"
    pool: str = "none"
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        self.events.append("acquire lock once")
        return True

    async def delete_prior_epoch_pins(self, active_epoch: UUID) -> int:
        del active_epoch
        raise AssertionError

    async def ping(self) -> bool:
        await anyio.sleep_forever()
        return True

    async def advisory_unlock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        self.events.append("unlock")
        return True

    async def close(self) -> None:
        self.events.append("close")


class _FailingCleanupConnection(_Connection):
    @override
    async def advisory_unlock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        self.events.append("unlock")
        raise RuntimeError

    @override
    async def close(self) -> None:
        self.events.append("close")
        raise RuntimeError


class _HangingCleanupConnection(_Connection):
    close_finished: anyio.Event
    close_release: anyio.Event
    unlock_finished: anyio.Event
    unlock_release: anyio.Event

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.unlock_release = anyio.Event()
        self.unlock_finished = anyio.Event()
        self.close_release = anyio.Event()
        self.close_finished = anyio.Event()

    @override
    async def advisory_unlock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        self.events.append("unlock")
        with anyio.CancelScope(shield=True):
            await self.unlock_release.wait()
        self.unlock_finished.set()
        return True

    @override
    async def close(self) -> None:
        self.events.append("close")
        with anyio.CancelScope(shield=True):
            await self.close_release.wait()
        self.close_finished.set()


class _Factory:
    connection: _Connection
    events: list[str]

    def __init__(self, connection: _Connection, events: list[str]) -> None:
        self.connection = connection
        self.events = events

    async def open(self) -> EpochSqlConnection:
        self.events.append("open dedicated connection")
        return self.connection


class _Cleaner:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def cleanup_prior_epoch_pins(
        self,
        connection: EpochSqlConnection,
        active_epoch: UUID,
    ) -> PriorEpochCleanup:
        del connection, active_epoch
        self.events.extend(
            ["same-connection transaction cleans prior epoch pins", "commit cleanup"]
        )
        return PriorEpochCleanup(0)


class _PrimaryFailingCleaner(_Cleaner):
    error: RuntimeError

    def __init__(self, events: list[str], error: RuntimeError) -> None:
        super().__init__(events)
        self.error = error

    @override
    async def cleanup_prior_epoch_pins(
        self,
        connection: EpochSqlConnection,
        active_epoch: UUID,
    ) -> PriorEpochCleanup:
        del connection, active_epoch
        self.events.append("primary cleanup failure")
        raise self.error


class _UuidSource:
    events: list[str]
    calls: int

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    def new(self) -> UUID:
        self.calls += 1
        self.events.append("create UUIDv4")
        return _EPOCH


class _Publisher:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def publish(self, epoch: UUID) -> None:
        del epoch
        self.events.append("publish epoch")


class _Readiness:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def set_ready(self, ready: bool) -> None:
        self.events.append(f"ready {str(ready).lower()}")


class _FailingReadiness(_Readiness):
    @override
    def set_ready(self, ready: bool) -> None:
        super().set_ready(ready)
        if not ready:
            raise RuntimeError


class _PublishFailingReadiness(_Readiness):
    primary: RuntimeError

    def __init__(self, events: list[str], primary: RuntimeError) -> None:
        super().__init__(events)
        self.primary = primary

    @override
    def set_ready(self, ready: bool) -> None:
        super().set_ready(ready)
        if ready:
            raise self.primary
        raise RuntimeError


class _Submitter:
    async def submit(self, event: MonitorEvent) -> None:
        del event


async def _startup_observation() -> tuple[list[str], int, UUID]:
    events: list[str] = []
    connection = _Connection(events)
    uuids = _UuidSource(events)
    coordinator = ServiceEpochCoordinator(
        ServiceEpochDependencies(
            connection_factory=_Factory(connection, events),
            pin_cleaner=_Cleaner(events),
            uuid_source=uuids,
            publisher=_Publisher(events),
            readiness=_Readiness(events),
        )
    )
    observed_epoch: UUID | None = None

    async with anyio.create_task_group() as tasks:
        runtime = await coordinator.start(tasks, _Submitter())
        observed_epoch = runtime.epoch
        runtime.monitor_scope.cancel()
        await runtime.monitor_joined.wait()
        tasks.cancel_scope.cancel()
    if observed_epoch is None:
        raise AssertionError
    return events, uuids.calls, observed_epoch


async def test_startup_uses_locked_connection_and_publishes_ready_last() -> None:
    events, uuid_calls, epoch = await _startup_observation()

    assert epoch == _EPOCH
    assert uuid_calls == 1
    assert events[:7] == [
        "open dedicated connection",
        "acquire lock once",
        "create UUIDv4",
        "same-connection transaction cleans prior epoch pins",
        "commit cleanup",
        "publish epoch",
        "ready true",
    ]


async def test_startup_failure_preserves_primary_and_attempts_all_cleanup() -> None:
    events: list[str] = []
    connection = _FailingCleanupConnection(events)
    primary = RuntimeError()
    coordinator = ServiceEpochCoordinator(
        ServiceEpochDependencies(
            connection_factory=_Factory(connection, events),
            pin_cleaner=_PrimaryFailingCleaner(events, primary),
            uuid_source=_UuidSource(events),
            publisher=_Publisher(events),
            readiness=_FailingReadiness(events),
        )
    )

    captured: pytest.ExceptionInfo[RuntimeError] | None = None
    async with anyio.create_task_group() as tasks:
        with pytest.raises(RuntimeError) as captured:
            _ = await coordinator.start(tasks, _Submitter())

    assert captured is not None
    assert captured.value is primary
    assert events[-4:] == ["primary cleanup failure", "ready false", "unlock", "close"]


async def test_readiness_publish_failure_joins_monitor_before_unlock_and_close() -> None:
    events: list[str] = []
    connection = _Connection(events)
    primary = RuntimeError()
    coordinator = ServiceEpochCoordinator(
        ServiceEpochDependencies(
            connection_factory=_Factory(connection, events),
            pin_cleaner=_Cleaner(events),
            uuid_source=_UuidSource(events),
            publisher=_Publisher(events),
            readiness=_PublishFailingReadiness(events, primary),
        )
    )
    captured: pytest.ExceptionInfo[RuntimeError] | None = None

    async with anyio.create_task_group() as tasks:
        with pytest.raises(RuntimeError) as captured:
            _ = await coordinator.start(tasks, _Submitter())

    assert captured is not None
    assert captured.value is primary
    assert events[-3:] == ["ready false", "unlock", "close"]


async def test_startup_cleanup_inner_shields_are_hard_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epoch_module, "STARTUP_CLEANUP_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    connection = _HangingCleanupConnection(events)
    primary = RuntimeError()
    coordinator = ServiceEpochCoordinator(
        ServiceEpochDependencies(
            connection_factory=_Factory(connection, events),
            pin_cleaner=_PrimaryFailingCleaner(events, primary),
            uuid_source=_UuidSource(events),
            publisher=_Publisher(events),
            readiness=_Readiness(events),
        )
    )
    captured: pytest.ExceptionInfo[RuntimeError] | None = None

    with anyio.fail_after(0.2):
        async with anyio.create_task_group() as tasks:
            with pytest.raises(RuntimeError) as captured:
                _ = await coordinator.start(tasks, _Submitter())

    assert captured is not None
    assert captured.value is primary
    assert events[-3:] == ["ready false", "unlock", "close"]
    assert not connection.unlock_finished.is_set()
    assert not connection.close_finished.is_set()
    connection.unlock_release.set()
    connection.close_release.set()
    with anyio.fail_after(1):
        await connection.unlock_finished.wait()
        await connection.close_finished.wait()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("autocommit", False),
        ("autocommit", 1),
        ("dedicated", False),
        ("dedicated", object()),
        ("driver", "asyncpg"),
        ("driver", object()),
        ("pool", "shared"),
        ("pool", object()),
    ],
    ids=(
        "autocommit_false",
        "autocommit_truthy_int",
        "dedicated_false",
        "dedicated_truthy_object",
        "wrong_driver",
        "driver_wrong_type",
        "pooled",
        "pool_wrong_type",
    ),
)
async def test_invalid_open_connection_close_inner_shield_is_hard_bounded(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    monkeypatch.setattr(epoch_module, "STARTUP_CLEANUP_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    connection = _HangingCleanupConnection(events)
    setattr(connection, field, value)
    coordinator = ServiceEpochCoordinator(
        ServiceEpochDependencies(
            connection_factory=_Factory(connection, events),
            pin_cleaner=_Cleaner(events),
            uuid_source=_UuidSource(events),
            publisher=_Publisher(events),
            readiness=_Readiness(events),
        )
    )

    with anyio.fail_after(0.2):
        with pytest.raises(ServiceEpochLockUnavailableError):
            _ = await coordinator.open()

    assert events == ["open dedicated connection", "close"]
    assert not connection.close_finished.is_set()
    connection.close_release.set()
    with anyio.fail_after(1):
        await connection.close_finished.wait()


@pytest.mark.parametrize("field", ["autocommit", "dedicated", "driver", "pool"])
async def test_invalid_open_metadata_exception_still_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    events: list[str] = []
    connection = _Connection(events)

    def fail_metadata(_connection: _Connection) -> object:
        raise RuntimeError

    monkeypatch.setattr(_Connection, field, property(fail_metadata))
    coordinator = ServiceEpochCoordinator(
        ServiceEpochDependencies(
            connection_factory=_Factory(connection, events),
            pin_cleaner=_Cleaner(events),
            uuid_source=_UuidSource(events),
            publisher=_Publisher(events),
            readiness=_Readiness(events),
        )
    )

    with pytest.raises(ServiceEpochLockUnavailableError) as captured:
        _ = await coordinator.open()

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert events == ["open dedicated connection", "close"]


_AXES = (
    "service_epoch_epoch_count_per_process",
    "service_epoch_epoch_kind",
    *(f"service_epoch_startup_order_{index}" for index in range(7)),
)


@pytest.mark.parametrize("axis", _AXES, ids=_AXES)
async def test_atomic_accepted_axis(axis: str) -> None:
    events, uuid_calls, epoch = await _startup_observation()
    observations: dict[str, object] = {
        "service_epoch_epoch_count_per_process": uuid_calls,
        "service_epoch_epoch_kind": "UUIDv4" if epoch.version == 4 else "other",
        **{f"service_epoch_startup_order_{index}": value for index, value in enumerate(events[:7])},
    }
    expected: dict[str, object] = {
        "service_epoch_epoch_count_per_process": 1,
        "service_epoch_epoch_kind": "UUIDv4",
        **{
            f"service_epoch_startup_order_{index}": value
            for index, value in enumerate(
                (
                    "open dedicated connection",
                    "acquire lock once",
                    "create UUIDv4",
                    "same-connection transaction cleans prior epoch pins",
                    "commit cleanup",
                    "publish epoch",
                    "ready true",
                )
            )
        },
    }
    assert observations[axis] == expected[axis]
