"""One lifecycle gate for monitor fail-stop, health, and admin overview."""

from dataclasses import dataclass
from uuid import uuid4

import pytest

from nvidia_build_lb.admin.schemas import OverviewStatus
from nvidia_build_lb.runtime_readiness import (
    MONITOR_FATAL_GRACE_SECONDS,
    GatedCredentialRepositories,
    LifecycleMonitorSubmitter,
    LifecycleReadinessProbe,
    RuntimeReadinessGate,
)
from nvidia_build_lb.service_epoch_monitor import (
    MonitorCancelled,
    MonitorFatal,
    MonitorFatalClass,
    MonitorHealthy,
)
from tests.contracts.fakes import contract_services

pytestmark = pytest.mark.anyio


@dataclass(slots=True)
class _FailStop:
    calls: int = 0
    events: list[str] | None = None

    def trigger(self) -> None:
        self.calls += 1
        if self.events is not None:
            self.events.append("fail_stop")


@dataclass(slots=True)
class _Sleeper:
    calls: list[float]
    events: list[str] | None = None

    async def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.events is not None:
            self.events.append("grace")


@dataclass(slots=True)
class _Readiness:
    ready: bool
    events: list[str]

    def set_ready(self, ready: bool) -> None:
        self.ready = ready
        self.events.append(f"readiness:{str(ready).lower()}")


@dataclass(slots=True)
class _Probe:
    ready: bool
    calls: int = 0

    async def is_ready(self) -> bool:
        self.calls += 1
        return self.ready


async def test_lifecycle_gate_forces_admin_overview_degraded_until_epoch_ready() -> None:
    services, repository_ready = contract_services()
    repository_ready.set_ready(True)
    gate = RuntimeReadinessGate()
    repositories = GatedCredentialRepositories(services.credentials.repositories, gate)

    closed = await repositories.overview()
    gate.set_ready(True)
    opened = await repositories.overview()

    assert closed.ready is False
    assert closed.status is OverviewStatus.DEGRADED
    assert opened.ready is True
    assert opened.status is OverviewStatus.OK


async def test_repository_degradation_still_wins_when_lifecycle_is_ready() -> None:
    services, repository_ready = contract_services()
    gate = RuntimeReadinessGate()
    gate.set_ready(True)
    repository_ready.set_ready(False)

    overview = await GatedCredentialRepositories(
        services.credentials.repositories,
        gate,
    ).overview()

    assert overview.ready is False
    assert overview.status is OverviewStatus.DEGRADED


async def test_lifecycle_readiness_probe_short_circuits_database_after_withdrawal() -> None:
    lifecycle = RuntimeReadinessGate()
    inner = _Probe(ready=True)
    probe = LifecycleReadinessProbe(lifecycle, inner)

    closed = await probe.is_ready()
    lifecycle.set_ready(True)
    opened = await probe.is_ready()

    assert closed is False
    assert opened is True
    assert inner.calls == 1


async def test_monitor_fatal_and_unclean_cancel_share_one_fail_stop() -> None:
    events: list[str] = []
    fail_stop = _FailStop(events=events)
    readiness = _Readiness(ready=True, events=events)
    sleeper = _Sleeper([], events)
    submitter = LifecycleMonitorSubmitter(readiness, fail_stop, sleeper)

    await submitter.submit(MonitorHealthy())
    await submitter.submit(MonitorFatal(MonitorFatalClass.SQL_ERROR))
    await submitter.submit(MonitorFatal(MonitorFatalClass.TIMEOUT))
    await submitter.submit(MonitorCancelled(None))

    assert fail_stop.calls == 1
    assert MONITOR_FATAL_GRACE_SECONDS == 2.0
    assert readiness.ready is False
    assert sleeper.calls == [MONITOR_FATAL_GRACE_SECONDS]
    assert events == ["readiness:false", "grace", "fail_stop"]


async def test_clean_monitor_cancel_does_not_trigger_fail_stop() -> None:
    fail_stop = _FailStop()
    readiness = RuntimeReadinessGate()
    readiness.set_ready(True)
    sleeper = _Sleeper([])
    submitter = LifecycleMonitorSubmitter(readiness, fail_stop, sleeper)

    await submitter.submit(MonitorCancelled(clean_token=uuid4()))

    assert fail_stop.calls == 0
    assert readiness.is_ready() is True
    assert sleeper.calls == []
