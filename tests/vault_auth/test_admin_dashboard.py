"""Coherent dashboard, readiness priority, and durable identity contracts."""

from dataclasses import dataclass, replace
from datetime import timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    AdminDashboardRead,
    AdminOperatorReadinessRead,
    AdminOverviewRead,
    CapacityBlocker,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    LedgerStatus,
    OverviewStatus,
    ReadinessCause,
    RuntimeState,
    UpstreamKeyCreateRequest,
)
from nvidia_build_lb.admin_credentials import CredentialRepositories
from nvidia_build_lb.admin_ledger import AdminLedger, AdminLedgerPolicy, LedgerCounts
from nvidia_build_lb.admin_mutation_barrier import AdminMutationBarrier
from nvidia_build_lb.api_types import RepositoryReadinessProbe
from nvidia_build_lb.attempt_repository import AttemptRepository, AttemptRepositoryDependencies
from nvidia_build_lb.attempt_types import AttemptFinalizeCommand, AttemptStartCommand
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import (
    EVENT_WRITER_GENERATION,
    AdminEventRow,
    AdminLedgerStateRow,
    UpstreamKeyRow,
)
from nvidia_build_lb.downstream_tokens import (
    DownstreamTokenDependencies,
    DownstreamTokenRepository,
    SystemTokenHexSource,
)
from nvidia_build_lb.main import create_app
from nvidia_build_lb.runtime_readiness import (
    GatedCredentialRepositories,
    LifecycleReadinessProbe,
    RuntimeReadinessGate,
)
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault
from tests.contracts._support import ADMIN_TOKEN, bearer
from tests.contracts.fakes import contract_services

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


@dataclass(frozen=True, slots=True)
class _FailStop:
    def trigger(self) -> None:
        """The expected commit path must never fail-stop."""


@dataclass(frozen=True, slots=True)
class _HttpTruth:
    health_status: int
    health_body: bytes
    overview: AdminOverviewRead
    dashboard: AdminDashboardRead
    operator_readiness: AdminOperatorReadinessRead


def _repositories(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    clock: Clock,
    policy: AdminLedgerPolicy | None = None,
) -> CredentialRepositories:
    ledger = AdminLedger(sessions, clock, policy or AdminLedgerPolicy())
    upstream = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock, ledger))
    downstream = DownstreamTokenRepository(
        DownstreamTokenDependencies(sessions, clock, SystemTokenHexSource(), ledger)
    )
    return CredentialRepositories(upstream, downstream, sessions, clock, ledger)


async def _enable_key(repositories: CredentialRepositories, credential: str) -> UUID:
    created = await repositories.upstream.create(
        UpstreamKeyCreateRequest(key=credential),
        request_id=f"create-{credential}",
    )
    async with repositories.sessions.begin() as session:
        row = await session.get(UpstreamKeyRow, created.id, with_for_update=True)
        assert row is not None
        row.health_state = HealthState.HEALTHY.value
    await repositories.upstream.enable(created.id, request_id=f"enable-{credential}")
    return created.id


def _http_truth(
    repositories: CredentialRepositories,
    *,
    runtime_ready: bool,
) -> _HttpTruth:
    base_services, _ = contract_services()
    lifecycle = RuntimeReadinessGate()
    lifecycle.set_ready(runtime_ready)
    barrier = AdminMutationBarrier()
    gated = GatedCredentialRepositories(repositories, lifecycle, barrier)
    credentials = replace(
        base_services.credentials,
        repositories=gated,
        lifecycle=lifecycle,
        mutation_barrier=barrier,
    )
    services = replace(
        base_services,
        credentials=credentials,
        readiness=LifecycleReadinessProbe(
            lifecycle,
            RepositoryReadinessProbe(gated),
        ),
    )
    with TestClient(create_app(services), base_url="http://127.0.0.1:2456") as client:
        health = client.get("/health")
        overview_response = client.get(
            "/admin/api/v1/overview",
            headers=bearer(ADMIN_TOKEN),
        )
        dashboard_response = client.get(
            "/admin/api/v1/dashboard",
            headers=bearer(ADMIN_TOKEN),
        )
        operator_readiness_response = client.get(
            "/admin/api/v1/operator-readiness",
            headers=bearer(ADMIN_TOKEN),
        )
    return _HttpTruth(
        health.status_code,
        health.content,
        AdminOverviewRead.model_validate_json(overview_response.content),
        AdminDashboardRead.model_validate_json(dashboard_response.content),
        AdminOperatorReadinessRead.model_validate_json(operator_readiness_response.content),
    )


async def test_dashboard_keeps_one_repeatable_read_snapshot_during_concurrent_commit(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = _repositories(migrated_session_factory, vault, fixed_clock)
    counts_entered = anyio.Event()
    allow_counts = anyio.Event()
    original_counts = AdminLedger.counts
    first_count = True

    async def pausing_counts(session: AsyncSession) -> LedgerCounts:
        nonlocal first_count
        if first_count:
            first_count = False
            counts_entered.set()
            await allow_counts.wait()
        return await original_counts(session)

    monkeypatch.setattr(AdminLedger, "counts", staticmethod(pausing_counts))
    snapshots: list[AdminDashboardRead] = []

    async def read_snapshot() -> None:
        snapshots.append(await repositories.dashboard())

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(read_snapshot)
        await counts_entered.wait()
        try:
            async with migrated_session_factory.begin() as session:
                session.add(
                    AdminEventRow(
                        id=uuid4(),
                        request_id="concurrent-dashboard-commit",
                        event_type=EventType.DOWNSTREAM_ISSUED.value,
                        upstream_key_id=None,
                        upstream_key_fingerprint=None,
                        downstream_token_id=None,
                        outcome_class=EventOutcome.SUCCEEDED.value,
                        status_class=None,
                        latency_ms=None,
                        occurred_at=fixed_clock.now(),
                        attempt_started_event_id=None,
                        writer_generation=EVENT_WRITER_GENERATION,
                    )
                )
        finally:
            allow_counts.set()

    assert len(snapshots) == 1
    concurrent = snapshots[0]
    assert concurrent.events.items == ()
    assert concurrent.ledger.event_rows == 0
    assert concurrent.overview.last_event_at is None

    refreshed = await repositories.dashboard()
    assert refreshed.ledger.event_rows == 1
    assert refreshed.events.items[0].request_id == "concurrent-dashboard-commit"
    assert refreshed.overview.last_event_at == refreshed.events.items[0].occurred_at


async def test_dashboard_preserves_deleted_key_identity_and_exact_attempt_link(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    repositories = _repositories(migrated_session_factory, vault, fixed_clock)
    key_id = await _enable_key(repositories, "dashboard-link-key")
    key = (await repositories.upstream.list_all()).items[0]
    started_event_id = uuid4()
    terminal_event_id = uuid4()
    attempts = AttemptRepository(
        AttemptRepositoryDependencies(
            migrated_session_factory,
            vault,
            fixed_clock,
            _FailStop(),
            repositories.resolved_ledger(),
        )
    )
    lease = await attempts.reserve_attempt(
        AttemptStartCommand(
            started_event_id=started_event_id,
            terminal_event_id=terminal_event_id,
            request_id="dashboard-linked-attempt",
            service_epoch=uuid4(),
            explicit_probe_key_id=None,
            excluded_key_ids=frozenset(),
            started_at=fixed_clock.now(),
        )
    )
    _ = await attempts.finalize_attempt(
        AttemptFinalizeCommand(
            identity=lease.identity,
            outcome=TerminalOutcome.SUCCEEDED,
            status_class=LastStatusClass.SUCCESS,
            latency_ms=7,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=fixed_clock.now() + timedelta(seconds=1),
        )
    )
    await repositories.upstream.disable(key_id, request_id="dashboard-link-disable")
    await repositories.upstream.delete(key_id, request_id="dashboard-link-delete")

    dashboard = await repositories.dashboard()
    by_id = {event.id: event for event in dashboard.events.items}
    started = by_id[started_event_id]
    terminal = by_id[terminal_event_id]
    deleted = next(
        event
        for event in dashboard.events.items
        if event.event_type is EventType.UPSTREAM_KEY_DELETED
    )

    assert dashboard.upstream_keys.items == ()
    assert dashboard.overview.request_count == 1
    assert started.upstream_key_id == key_id
    assert started.upstream_key_fingerprint == key.fingerprint
    assert started.attempt_started_event_id is None
    assert terminal.upstream_key_id == key_id
    assert terminal.upstream_key_fingerprint == key.fingerprint
    assert terminal.attempt_started_event_id == started.id
    assert deleted.upstream_key_id == key_id
    assert deleted.upstream_key_fingerprint == key.fingerprint


async def test_dashboard_keeps_permanent_evidence_blocker_closed_with_free_row_capacity(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    repositories = _repositories(migrated_session_factory, vault, fixed_clock)
    _ = await _enable_key(repositories, "dashboard-durable-blocker-key")
    async with migrated_session_factory.begin() as session:
        ledger = await session.get(AdminLedgerStateRow, 1, with_for_update=True)
        assert ledger is not None
        ledger.last_capacity_blocker = CapacityBlocker.ORPHANED_PENDING.value

    dashboard = await repositories.dashboard()

    assert dashboard.ledger.event_rows < dashboard.ledger.event_capacity
    assert dashboard.ledger.attempt_rows < dashboard.ledger.attempt_capacity
    assert dashboard.ledger.status is LedgerStatus.CAPACITY_BLOCKED
    assert dashboard.ledger.capacity_blocker is CapacityBlocker.ORPHANED_PENDING
    assert dashboard.readiness_cause is ReadinessCause.LEDGER_CAPACITY_EXHAUSTED
    assert dashboard.overview.ready is False


async def test_runtime_capacity_and_eligibility_share_one_http_readiness_priority(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    normal = _repositories(migrated_session_factory, vault, fixed_clock)
    key_id = await _enable_key(normal, "dashboard-readiness-key")
    constrained = _repositories(
        migrated_session_factory,
        vault,
        fixed_clock,
        AdminLedgerPolicy(event_max_rows=2, attempt_max_rows=40_000),
    )

    ready = _http_truth(normal, runtime_ready=True)
    capacity = _http_truth(constrained, runtime_ready=True)
    runtime = _http_truth(constrained, runtime_ready=False)
    await normal.upstream.disable(key_id, request_id="dashboard-readiness-disable")
    eligibility = _http_truth(normal, runtime_ready=True)

    assert ready.health_status == 200
    assert ready.health_body == b'{"status":"ok","ready":true}'
    assert ready.overview.ready is True
    assert ready.overview.status is OverviewStatus.OK
    assert ready.dashboard.runtime_state is RuntimeState.OPERATIONAL
    assert ready.dashboard.readiness_cause is ReadinessCause.READY
    assert ready.operator_readiness.runtime_state is RuntimeState.OPERATIONAL
    assert ready.operator_readiness.readiness_cause is ReadinessCause.READY

    assert capacity.health_status == 503
    assert capacity.health_body == b'{"status":"degraded","ready":false}'
    assert capacity.overview.ready is False
    assert capacity.overview.status is OverviewStatus.DEGRADED
    assert capacity.dashboard.runtime_state is RuntimeState.OPERATIONAL
    assert capacity.dashboard.readiness_cause is ReadinessCause.LEDGER_CAPACITY_EXHAUSTED
    assert capacity.operator_readiness.ledger_status is LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING
    assert capacity.operator_readiness.readiness_cause is ReadinessCause.LEDGER_CAPACITY_EXHAUSTED

    assert runtime.health_status == 503
    assert runtime.overview.ready is False
    assert runtime.dashboard.runtime_state is RuntimeState.UNAVAILABLE
    assert runtime.dashboard.readiness_cause is ReadinessCause.RUNTIME_UNAVAILABLE
    assert runtime.operator_readiness.runtime_state is RuntimeState.UNAVAILABLE
    assert runtime.operator_readiness.readiness_cause is ReadinessCause.RUNTIME_UNAVAILABLE

    assert eligibility.health_status == 503
    assert eligibility.overview.ready is False
    assert eligibility.dashboard.runtime_state is RuntimeState.OPERATIONAL
    assert eligibility.dashboard.readiness_cause is ReadinessCause.NO_ELIGIBLE_UPSTREAM
    assert eligibility.operator_readiness.readiness_cause is ReadinessCause.NO_ELIGIBLE_UPSTREAM
