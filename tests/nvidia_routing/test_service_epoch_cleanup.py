"""Post-clean unlock/close matrix and external watchdog failure receipts."""

from datetime import UTC, datetime
from typing import Never, override
from uuid import UUID

import anyio
import pytest

import nvidia_build_lb.service_epoch as epoch_module
import nvidia_build_lb.service_epoch_cleanup as cleanup_module
from nvidia_build_lb.service_epoch import ServiceEpochRuntime, run_supervised_epoch_subprocess
from nvidia_build_lb.service_epoch_cleanup import (
    CleanupDependencies,
    ExitDisposition,
    ExitKind,
    HandoffResult,
)
from nvidia_build_lb.service_epoch_monitor import CleanStopState
from nvidia_build_lb.service_epoch_watchdog import (
    CleanupExitWatchdog,
    WatchdogFailure,
    WatchdogObservation,
)

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_EPOCH = UUID("00000000-0000-4000-8000-000000000401")
_NONCE = UUID("00000000-0000-4000-8000-000000000402")
_NOW = datetime(2026, 7, 13, 0, 0, 5, tzinfo=UTC)


class _ExitError(Exception):
    status: int

    def __init__(self, status: int) -> None:
        super().__init__()
        self.status = status


class _Connection:
    autocommit: bool = True
    dedicated: bool = True
    driver: str = "psycopg_async"
    pool: str = "none"
    events: list[str]
    unlock_outcome: str
    close_outcome: str

    def __init__(self, events: list[str], unlock_outcome: str, close_outcome: str) -> None:
        self.events = events
        self.unlock_outcome = unlock_outcome
        self.close_outcome = close_outcome

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        return True

    async def delete_prior_epoch_pins(self, active_epoch: UUID) -> int:
        del active_epoch
        return 0

    async def ping(self) -> bool:
        return True

    async def advisory_unlock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        self.events.append("unlock")
        if self.unlock_outcome == "hang":
            await anyio.sleep_forever()
        if self.unlock_outcome == "exception":
            raise OSError
        return self.unlock_outcome == "true"

    async def close(self) -> None:
        self.events.append("close")
        if self.close_outcome == "hang":
            await anyio.sleep_forever()
        if self.close_outcome == "exception":
            raise OSError


class _Committer:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def commit_stopping(self, epoch: UUID) -> None:
        assert epoch == _EPOCH
        self.events.append("commit")


class _HangingCommitter:
    async def commit_stopping(self, epoch: UUID) -> None:
        del epoch
        await anyio.sleep_forever()


class _ShieldedHangingCommitter:
    events: list[str]
    finished: anyio.Event
    release: anyio.Event

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.finished = anyio.Event()
        self.release = anyio.Event()

    async def commit_stopping(self, epoch: UUID) -> None:
        del epoch
        self.events.append("commit")
        with anyio.CancelScope(shield=True):
            await self.release.wait()
        self.finished.set()


class _ShieldedHangingConnection(_Connection):
    close_finished: anyio.Event
    close_release: anyio.Event
    unlock_finished: anyio.Event
    unlock_release: anyio.Event

    def __init__(self, events: list[str]) -> None:
        super().__init__(events, "true", "success")
        self.close_finished = anyio.Event()
        self.close_release = anyio.Event()
        self.unlock_finished = anyio.Event()
        self.unlock_release = anyio.Event()

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


class _Handoff:
    disposition: ExitDisposition | None = None
    events: list[str]
    result: HandoffResult

    def __init__(self, events: list[str], result: HandoffResult = HandoffResult.ACK) -> None:
        self.events = events
        self.result = result

    async def send(self, disposition: ExitDisposition) -> HandoffResult:
        self.events.append("handoff")
        self.disposition = disposition
        return self.result


class _ShieldedHangingHandoff(_Handoff):
    disposition: ExitDisposition | None
    finished: anyio.Event
    release: anyio.Event

    def __init__(self, events: list[str]) -> None:
        super().__init__(events)
        self.finished = anyio.Event()
        self.release = anyio.Event()

    @override
    async def send(self, disposition: ExitDisposition) -> HandoffResult:
        self.events.append("handoff")
        self.disposition = disposition
        with anyio.CancelScope(shield=True):
            await self.release.wait()
        self.finished.set()
        return HandoffResult.ACK


class _Terminator:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def exit(self, status: int) -> Never:
        self.events.append(f"exit:{status}")
        raise _ExitError(status)


class _LateFatal:
    value: bool

    def __init__(self, *, present: bool) -> None:
        self.value = present

    def present(self) -> bool:
        return self.value


def _runtime(connection: _Connection) -> ServiceEpochRuntime:
    joined = anyio.Event()
    joined.set()
    return ServiceEpochRuntime(
        epoch=_EPOCH,
        connection=connection,
        clean_stop=CleanStopState(),
        monitor_scope=anyio.CancelScope(),
        monitor_joined=joined,
    )


def _cleanup_dependencies(
    events: list[str],
    handoff: _Handoff,
    *,
    late: bool,
) -> CleanupDependencies:
    return CleanupDependencies(
        nonce=_NONCE,
        committer=_Committer(events),
        handoff=handoff,
        terminator=_Terminator(events),
        late_fatal=_LateFatal(present=late),
    )


def _cleanup_case_id(unlock: str, close: str, late: bool) -> str:
    if not late:
        return f"unlock_{unlock}__close_{close}__late_none"
    checkpoint = "after_close_exception" if close == "exception" else "after_close_result"
    return f"unlock_{unlock}__close_{close}__late_{checkpoint}_before_process_local_disposition"


_CLEANUP_CASES = tuple(
    pytest.param(
        (unlock, close, late),
        id=_cleanup_case_id(unlock, close, late),
    )
    for unlock in ("true", "false", "exception")
    for close in ("success", "exception")
    for late in (False, True)
)


async def _test_cleanup_case(case: tuple[str, str, bool]) -> None:
    unlock, close, late = case
    events: list[str] = []
    connection = _Connection(events, unlock, close)
    handoff = _Handoff(events)
    expected_status = (
        79
        if late
        else 74
        if close == "exception"
        else 73
        if unlock == "exception"
        else 72
        if unlock == "false"
        else 0
    )

    if expected_status:
        with pytest.raises(_ExitError) as raised:
            _ = await run_supervised_epoch_subprocess(
                _runtime(connection),
                _cleanup_dependencies(events, handoff, late=late),
            )
        assert raised.value.status == expected_status
    else:
        result = await run_supervised_epoch_subprocess(
            _runtime(connection),
            _cleanup_dependencies(events, handoff, late=late),
        )
        assert result.disposition.exit_status == 0
        assert result.unlock_attempts == result.close_attempts == result.handoff_attempts == 1
    assert events[:4] == ["commit", "unlock", "close", "handoff"]
    assert handoff.disposition is not None
    assert handoff.disposition.exit_status == expected_status


_FAILURE_CASES = (
    pytest.param("handoff_send_timeout", id="handoff_send_timeout"),
    pytest.param("handoff_nack", id="handoff_nack"),
    pytest.param("handoff_ack_timeout", id="handoff_ack_timeout"),
    pytest.param("watchdog_observation_timeout", id="watchdog_observation_timeout"),
    pytest.param("watchdog_receipt_write_failure", id="watchdog_receipt_write_failure"),
    pytest.param("watchdog_receipt_write_timeout", id="watchdog_receipt_write_timeout"),
    pytest.param("watchdog_receipt_fsync_failure", id="watchdog_receipt_fsync_failure"),
    pytest.param("watchdog_receipt_readback_failure", id="watchdog_receipt_readback_failure"),
)


async def _test_failure_case(case: str) -> None:
    if case.startswith("handoff_"):
        result = {
            "handoff_send_timeout": HandoffResult.SEND_TIMEOUT,
            "handoff_nack": HandoffResult.NACK,
            "handoff_ack_timeout": HandoffResult.ACK_TIMEOUT,
        }[case]
        events: list[str] = []
        handoff = _Handoff(events, result)
        with pytest.raises(_ExitError) as raised:
            _ = await run_supervised_epoch_subprocess(
                _runtime(_Connection(events, "true", "success")),
                _cleanup_dependencies(events, handoff, late=False),
            )
        assert raised.value.status == 75
        return
    failure = {
        "watchdog_observation_timeout": WatchdogFailure.OBSERVATION_TIMEOUT,
        "watchdog_receipt_write_failure": WatchdogFailure.WRITE_FAILURE,
        "watchdog_receipt_write_timeout": WatchdogFailure.WRITE_TIMEOUT,
        "watchdog_receipt_fsync_failure": WatchdogFailure.FSYNC_FAILURE,
        "watchdog_receipt_readback_failure": WatchdogFailure.READBACK_FAILURE,
    }[case]
    disposition = ExitDisposition(
        kind=ExitKind.STOPPED,
        mode="clean",
        primary_reason=None,
        cleanup_causes=(),
        exit_status=0,
        epoch_id=_EPOCH,
        nonce=_NONCE,
    )
    result = CleanupExitWatchdog().evaluate(
        WatchdogObservation(
            disposition=disposition,
            epoch_id=_EPOCH,
            nonce=_NONCE,
            child_exit_status=0,
            observed_at=_NOW,
            failure=failure,
        )
    )
    assert result.candidate.service_transition_writes == 0
    if failure is WatchdogFailure.OBSERVATION_TIMEOUT:
        assert result.exit_status == 77
        assert result.durable_receipt is not None
    else:
        assert result.exit_status == 76
        assert result.durable_receipt is None


@pytest.mark.parametrize("case", [*_CLEANUP_CASES, *_FAILURE_CASES])
async def test_post_clean_cleanup(case: tuple[str, str, bool] | str) -> None:
    if isinstance(case, tuple):
        await _test_cleanup_case(case)
        return
    await _test_failure_case(case)


@pytest.mark.parametrize(
    ("failure", "exit_status", "durable", "absence"),
    [
        pytest.param(
            WatchdogFailure.OBSERVATION_TIMEOUT,
            77,
            True,
            None,
            id="observation_timeout",
        ),
        pytest.param(WatchdogFailure.WRITE_FAILURE, 76, False, True, id="receipt_failure"),
    ],
)
async def test_watchdog_failure_precedes_missing_disposition_without_false_absence(
    failure: WatchdogFailure,
    exit_status: int,
    durable: bool,
    absence: bool | None,
) -> None:
    result = CleanupExitWatchdog().evaluate(
        WatchdogObservation(
            disposition=None,
            epoch_id=_EPOCH,
            nonce=_NONCE,
            child_exit_status=75,
            observed_at=_NOW,
            failure=failure,
            missing_disposition_reason="rejected_disposition",
        )
    )

    assert result.exit_status == exit_status
    assert (result.durable_receipt is not None) is durable
    assert result.candidate.verdict == failure.value
    assert result.candidate.postgres_backend_absent is absence
    assert result.candidate.advisory_lock_absent is absence


async def test_monitor_join_is_bounded_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(epoch_module, "MONITOR_JOIN_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    runtime = ServiceEpochRuntime(
        epoch=_EPOCH,
        connection=_Connection(events, "true", "success"),
        clean_stop=CleanStopState(),
        monitor_scope=anyio.CancelScope(),
        monitor_joined=anyio.Event(),
    )

    with pytest.raises(TimeoutError):
        _ = await run_supervised_epoch_subprocess(
            runtime,
            _cleanup_dependencies(events, _Handoff(events), late=False),
        )
    assert events == []


async def test_commit_is_bounded_before_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cleanup_module, "CLEANUP_IO_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    dependencies = CleanupDependencies(
        nonce=_NONCE,
        committer=_HangingCommitter(),
        handoff=_Handoff(events),
        terminator=_Terminator(events),
        late_fatal=_LateFatal(present=False),
    )

    with pytest.raises(TimeoutError):
        _ = await run_supervised_epoch_subprocess(
            _runtime(_Connection(events, "true", "success")),
            dependencies,
        )
    assert events == []


async def test_unlock_and_close_timeouts_become_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cleanup_module, "CLEANUP_IO_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    handoff = _Handoff(events)

    with pytest.raises(_ExitError) as raised:
        _ = await run_supervised_epoch_subprocess(
            _runtime(_Connection(events, "hang", "hang")),
            _cleanup_dependencies(events, handoff, late=False),
        )

    assert raised.value.status == 74
    assert events == ["commit", "unlock", "close", "handoff", "exit:74"]


async def test_commit_inner_shield_is_hard_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cleanup_module, "CLEANUP_IO_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    committer = _ShieldedHangingCommitter(events)
    dependencies = CleanupDependencies(
        nonce=_NONCE,
        committer=committer,
        handoff=_Handoff(events),
        terminator=_Terminator(events),
        late_fatal=_LateFatal(present=False),
    )

    with anyio.fail_after(0.2):
        with pytest.raises(TimeoutError):
            _ = await run_supervised_epoch_subprocess(
                _runtime(_Connection(events, "true", "success")),
                dependencies,
            )

    assert events == ["commit"]
    assert not committer.finished.is_set()
    committer.release.set()
    with anyio.fail_after(1):
        await committer.finished.wait()


async def test_unlock_and_close_inner_shields_are_hard_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cleanup_module, "CLEANUP_IO_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    connection = _ShieldedHangingConnection(events)

    with anyio.fail_after(0.2):
        with pytest.raises(_ExitError) as raised:
            _ = await run_supervised_epoch_subprocess(
                _runtime(connection),
                _cleanup_dependencies(events, _Handoff(events), late=False),
            )

    assert raised.value.status == 74
    assert events == ["commit", "unlock", "close", "handoff", "exit:74"]
    assert not connection.unlock_finished.is_set()
    assert not connection.close_finished.is_set()
    connection.unlock_release.set()
    connection.close_release.set()
    with anyio.fail_after(1):
        await connection.unlock_finished.wait()
        await connection.close_finished.wait()


async def test_handoff_inner_shield_is_hard_bounded_to_exit_75(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cleanup_module, "CLEANUP_IO_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    handoff = _ShieldedHangingHandoff(events)

    with anyio.fail_after(0.2):
        with pytest.raises(_ExitError) as raised:
            _ = await run_supervised_epoch_subprocess(
                _runtime(_Connection(events, "true", "success")),
                _cleanup_dependencies(events, handoff, late=False),
            )

    assert raised.value.status == 75
    assert events == ["commit", "unlock", "close", "handoff", "exit:75"]
    assert not handoff.finished.is_set()
    handoff.release.set()
    with anyio.fail_after(1):
        await handoff.finished.wait()
