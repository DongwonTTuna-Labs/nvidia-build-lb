"""One lifecycle gate for monitor fail-stop, health, and admin overview."""

from dataclasses import dataclass
from uuid import uuid4

import pytest

from nvidia_build_lb.admin.schemas import OverviewStatus
from nvidia_build_lb.runtime_readiness import (
    GatedCredentialRepositories,
    LifecycleMonitorSubmitter,
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

    def trigger(self) -> None:
        self.calls += 1


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


async def test_monitor_fatal_and_unclean_cancel_share_one_fail_stop() -> None:
    fail_stop = _FailStop()
    submitter = LifecycleMonitorSubmitter(fail_stop)

    await submitter.submit(MonitorHealthy())
    await submitter.submit(MonitorFatal(MonitorFatalClass.SQL_ERROR))
    await submitter.submit(MonitorFatal(MonitorFatalClass.TIMEOUT))
    await submitter.submit(MonitorCancelled(None))

    assert fail_stop.calls == 1


async def test_clean_monitor_cancel_does_not_trigger_fail_stop() -> None:
    fail_stop = _FailStop()
    submitter = LifecycleMonitorSubmitter(fail_stop)

    await submitter.submit(MonitorCancelled(clean_token=uuid4()))

    assert fail_stop.calls == 0
