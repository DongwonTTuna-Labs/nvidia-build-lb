"""Startup and recurring maintenance lifecycle sensors."""

import anyio
import pytest

from nvidia_build_lb.admin_maintenance import AdminMaintenanceWorker
from nvidia_build_lb.admin_maintenance_types import MaintenanceResult

pytestmark = pytest.mark.anyio


class _FailingPass:
    calls: int

    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self) -> MaintenanceResult:
        self.calls += 1
        raise RuntimeError


class _Reporter:
    calls: int
    reported: anyio.Event

    def __init__(self) -> None:
        self.calls = 0
        self.reported = anyio.Event()

    def __call__(self) -> None:
        self.calls += 1
        self.reported.set()


class _IntervalSleeper:
    calls: int
    waiting_again: anyio.Event

    def __init__(self) -> None:
        self.calls = 0
        self.waiting_again = anyio.Event()

    async def sleep(self, seconds: float) -> None:
        assert seconds == 10
        self.calls += 1
        if self.calls == 1:
            return
        self.waiting_again.set()
        await anyio.sleep_forever()


async def test_recurring_failure_waits_a_full_interval_before_another_pass() -> None:
    maintenance = _FailingPass()
    reporter = _Reporter()
    sleeper = _IntervalSleeper()
    worker = AdminMaintenanceWorker(maintenance, 10, reporter, sleeper)

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(worker.run)
        with anyio.fail_after(1):
            await reporter.reported.wait()
            await sleeper.waiting_again.wait()
        tasks.cancel_scope.cancel()

    assert maintenance.calls == 1
    assert reporter.calls == 1
    assert sleeper.calls == 2
