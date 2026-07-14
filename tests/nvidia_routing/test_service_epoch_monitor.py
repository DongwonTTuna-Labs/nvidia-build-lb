"""Typed monitor observations and clean cancellation token."""

from uuid import UUID

import anyio
import pytest
from anyio.lowlevel import checkpoint

import nvidia_build_lb.service_epoch_monitor as monitor_module
from nvidia_build_lb.service_epoch_monitor import (
    MONITOR_INTERVAL_SECONDS,
    MONITOR_TIMEOUT_SECONDS,
    CleanStopState,
    MonitorCancelled,
    MonitorEvent,
    MonitorFatal,
    MonitorFatalClass,
    MonitorFatalLatch,
    MonitorHealthy,
    ServiceEpochMonitor,
)
from nvidia_build_lb.service_epoch_types import EpochConnectionUnexpectedCloseError

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _Connection:
    async def ping(self) -> bool:
        return True


class _Sleeper:
    calls: int = 0

    async def sleep(self, seconds: float) -> None:
        del seconds
        self.calls += 1
        if self.calls > 1:
            await anyio.sleep_forever()


class _Submitter:
    events: list[MonitorEvent]

    def __init__(self) -> None:
        self.events = []

    async def submit(self, event: MonitorEvent) -> None:
        self.events.append(event)


class _ImmediateSleeper:
    async def sleep(self, seconds: float) -> None:
        del seconds


class _CheckpointSleeper:
    async def sleep(self, seconds: float) -> None:
        del seconds
        await checkpoint()


class _HangingSubmitter:
    async def submit(self, event: MonitorEvent) -> None:
        del event
        await anyio.sleep_forever()


class _RaisingSubmitter:
    error: OSError

    def __init__(self, error: OSError) -> None:
        self.error = error

    async def submit(self, event: MonitorEvent) -> None:
        del event
        raise self.error


async def _run_cancelled(monitor: ServiceEpochMonitor) -> None:
    with anyio.CancelScope() as scope:
        scope.cancel()
        await monitor.run()


async def test_monitor_publishes_health_then_exact_clean_cancel_token() -> None:
    submitter = _Submitter()
    clean_stop = CleanStopState()
    monitor = ServiceEpochMonitor(_Connection(), submitter, _Sleeper(), clean_stop)
    token = UUID("00000000-0000-4000-8000-000000000402")

    async with anyio.create_task_group() as tasks:
        scope = anyio.CancelScope()

        async def run() -> None:
            with scope:
                await monitor.run()

        _ = tasks.start_soon(run)
        while not submitter.events:
            await checkpoint()
        clean_stop.request(token)
        scope.cancel()

    assert isinstance(submitter.events[0], MonitorHealthy)
    cancelled = submitter.events[-1]
    assert isinstance(cancelled, MonitorCancelled)
    assert cancelled.clean_token == token


async def test_monitor_submission_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor_module, "MONITOR_SUBMIT_TIMEOUT_SECONDS", 0.01)
    monitor = ServiceEpochMonitor(
        _Connection(),
        _HangingSubmitter(),
        _ImmediateSleeper(),
        CleanStopState(),
    )

    with pytest.raises(TimeoutError):
        await monitor.run()


async def test_sql_failure_is_not_submitter_error_context() -> None:
    sql_error = RuntimeError("sensitive SQL driver detail")
    submitter_error = OSError("safe submit failure")
    monitor = ServiceEpochMonitor(
        _FailureConnection(sql_error),
        _RaisingSubmitter(submitter_error),
        _ImmediateSleeper(),
        CleanStopState(),
    )

    with pytest.raises(OSError, match="safe submit failure") as captured:
        await monitor.run()

    assert captured.value is submitter_error
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


async def test_sql_failure_is_not_submit_timeout_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor_module, "MONITOR_SUBMIT_TIMEOUT_SECONDS", 0.01)
    sql_error = RuntimeError("sensitive SQL driver detail")
    monitor = ServiceEpochMonitor(
        _FailureConnection(sql_error),
        _HangingSubmitter(),
        _ImmediateSleeper(),
        CleanStopState(),
    )

    with pytest.raises(TimeoutError) as captured:
        await monitor.run()

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


class _FailureConnection:
    outcome: bool | Exception | None

    def __init__(self, outcome: bool | Exception | None) -> None:
        self.outcome = outcome

    async def ping(self) -> bool:
        if self.outcome is None:
            await anyio.sleep_forever()
            raise AssertionError
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(None, MonitorFatalClass.TIMEOUT, id="timeout"),
        pytest.param(OSError(), MonitorFatalClass.SQL_ERROR, id="sql_error"),
        pytest.param(False, MonitorFatalClass.UNEXPECTED_EOF, id="unexpected_eof"),
        pytest.param(
            EpochConnectionUnexpectedCloseError(),
            MonitorFatalClass.UNEXPECTED_CLOSE,
            id="unexpected_close",
        ),
    ],
)
async def test_monitor_submits_observed_failure_class(
    monkeypatch: pytest.MonkeyPatch,
    outcome: bool | Exception | None,
    expected: MonitorFatalClass,
) -> None:
    monkeypatch.setattr(monitor_module, "MONITOR_TIMEOUT_SECONDS", 0.01)
    submitter = _Submitter()
    monitor = ServiceEpochMonitor(
        _FailureConnection(outcome),
        submitter,
        _ImmediateSleeper(),
        CleanStopState(),
    )

    await monitor.run()

    assert submitter.events == [MonitorFatal(expected)]


async def test_monitor_cancel_without_clean_token_is_typed_nonfatal_candidate() -> None:
    submitter = _Submitter()
    monitor = ServiceEpochMonitor(_Connection(), submitter, _Sleeper(), CleanStopState())
    async with anyio.create_task_group() as tasks:
        scope = anyio.CancelScope()

        async def run() -> None:
            with scope:
                await monitor.run()

        _ = tasks.start_soon(run)
        while not submitter.events:
            await checkpoint()
        scope.cancel()

    assert isinstance(submitter.events[-1], MonitorCancelled)
    assert submitter.events[-1].clean_token is None
    assert not any(isinstance(event, MonitorFatal) for event in submitter.events)


async def test_cancelled_monitor_submitter_error_has_no_cancel_context() -> None:
    submitter_error = OSError("safe cancelled submit failure")
    monitor = ServiceEpochMonitor(
        _Connection(),
        _RaisingSubmitter(submitter_error),
        _CheckpointSleeper(),
        CleanStopState(),
    )

    with pytest.raises(OSError, match="safe cancelled submit failure") as captured:
        await _run_cancelled(monitor)

    assert captured.value is submitter_error
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


async def test_cancelled_monitor_submit_timeout_has_no_cancel_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(monitor_module, "MONITOR_SUBMIT_TIMEOUT_SECONDS", 0.01)
    clean_stop = CleanStopState()
    clean_stop.request(UUID("00000000-0000-4000-8000-000000000403"))
    monitor = ServiceEpochMonitor(
        _Connection(),
        _HangingSubmitter(),
        _CheckpointSleeper(),
        clean_stop,
    )

    with pytest.raises(TimeoutError) as captured:
        await _run_cancelled(monitor)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


async def test_monitor_fatal_latch_accepts_exactly_one_observation() -> None:
    latch = MonitorFatalLatch()
    first = MonitorFatal(MonitorFatalClass.SQL_ERROR)

    assert latch.register(first) is True
    assert latch.register(MonitorFatal(MonitorFatalClass.TIMEOUT)) is False
    assert latch.current() is first
    assert MONITOR_INTERVAL_SECONDS == 1
    assert MONITOR_TIMEOUT_SECONDS == 2
