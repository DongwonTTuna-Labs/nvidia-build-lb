"""Construct and retire the complete database-backed production service graph."""

import sys
from dataclasses import dataclass

import anyio
from anyio.abc import TaskGroup
from sqlalchemy.ext.asyncio import AsyncEngine

from nvidia_build_lb import service_epoch
from nvidia_build_lb.active_routed_requests import ActiveRoutedRequestRegistry
from nvidia_build_lb.admin_credentials import CredentialRepositories, CredentialServices
from nvidia_build_lb.admin_ledger import AdminLedger, AdminLedgerPolicy
from nvidia_build_lb.admin_maintenance import (
    AdminMaintenance,
    AdminMaintenancePrePublish,
)
from nvidia_build_lb.admin_mutation_barrier import AdminMutationBarrier
from nvidia_build_lb.api_streaming import LoggedResponderFactory
from nvidia_build_lb.api_types import ApplicationServices, RepositoryReadinessProbe
from nvidia_build_lb.attempt_fail_stop import LifecycleAttemptFailStop
from nvidia_build_lb.attempt_repository import AttemptRepository
from nvidia_build_lb.attempt_repository_dependencies import AttemptRepositoryDependencies
from nvidia_build_lb.auth import (
    AdminAuthenticator,
    CredentialAuthenticators,
    DownstreamAuthenticator,
)
from nvidia_build_lb.config import LogLevel, Settings
from nvidia_build_lb.db import create_engine, create_session_factory
from nvidia_build_lb.downstream_tokens import (
    DownstreamTokenDependencies,
    DownstreamTokenRepository,
    SystemTokenHexSource,
)
from nvidia_build_lb.logging import (
    LogEventName,
    LoggingConfig,
    SafeLogEvent,
    ServiceLogger,
    init_logging,
)
from nvidia_build_lb.main import create_app
from nvidia_build_lb.nvidia_adapter import NvidiaAdapterDependencies, NvidiaHostedAdapter
from nvidia_build_lb.production_lifecycle import (
    EpochPublisher,
    ProductionCleanup,
    close_production_resources,
    start_maintenance_worker,
)
from nvidia_build_lb.production_probe import RoutingProbeExecutor
from nvidia_build_lb.routing import RoutingCoordinator, RoutingCoordinatorDependencies
from nvidia_build_lb.runtime_primitives import (
    AnyioPollingSleeper,
    CancelScopeRootCancellation,
    SystemClock,
    SystemJitter,
    SystemUuidSource,
)
from nvidia_build_lb.runtime_readiness import (
    GatedCredentialRepositories,
    LifecycleMonitorSubmitter,
    LifecycleReadinessProbe,
    RuntimeReadinessGate,
)
from nvidia_build_lb.server_error_boundary import SafeServerErrorFastAPI
from nvidia_build_lb.service_epoch_connection import PsycopgEpochConnectionFactory
from nvidia_build_lb.transport_adapter import SanitizedAsyncClient
from nvidia_build_lb.upstream_key_dependencies import UpstreamKeyDependencies
from nvidia_build_lb.upstream_keys import UpstreamKeyRepository
from nvidia_build_lb.vault import Vault
from nvidia_build_lb.vault_key_binding import ensure_vault_key_binding

_MAINTENANCE_JOIN_TIMEOUT_SECONDS = 7.0
_PRODUCTION_CLEANUP_FAILED = "production_cleanup_failed"


@dataclass(frozen=True, slots=True)
class ProductionResources:
    """All application-lifetime resources owned by the process entrypoint."""

    app: SafeServerErrorFastAPI
    engine: AsyncEngine
    client: SanitizedAsyncClient
    epoch: service_epoch.ServiceEpochRuntime
    readiness: RuntimeReadinessGate
    logger: ServiceLogger
    maintenance_scope: anyio.CancelScope
    maintenance_joined: anyio.Event

    async def close(self) -> None:
        """Withdraw readiness and close every owned resource with bounded cleanup."""
        closed = await close_production_resources(
            ProductionCleanup(
                self.readiness,
                self.engine,
                self.client,
                self.epoch,
                self.maintenance_scope,
                self.maintenance_joined,
                _MAINTENANCE_JOIN_TIMEOUT_SECONDS,
            )
        )
        if not closed:
            raise RuntimeError(_PRODUCTION_CLEANUP_FAILED)


async def build_production_resources(
    settings: Settings,
    tasks: TaskGroup,
    root_scope: anyio.CancelScope,
) -> ProductionResources:
    """Build one real repository, epoch, NVIDIA, auth, and HTTP graph."""
    clock, jitter, uuids = SystemClock(), SystemJitter(), SystemUuidSource()
    readiness = RuntimeReadinessGate()
    mutation_barrier = AdminMutationBarrier()
    fail_stop = LifecycleAttemptFailStop(
        readiness,
        CancelScopeRootCancellation(root_scope),
    )
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    vault = Vault(settings.vault_key)
    active_requests = ActiveRoutedRequestRegistry()
    ledger = AdminLedger(
        sessions,
        clock,
        AdminLedgerPolicy(
            event_retention_days=settings.admin_event_retention_days,
            event_max_rows=settings.admin_event_max_rows,
            attempt_max_rows=settings.admin_attempt_max_rows,
            prune_batch_size=settings.admin_ledger_prune_batch_size,
            maintenance_interval_seconds=settings.admin_ledger_maintenance_interval_seconds,
            reconciliation_grace_seconds=settings.admin_attempt_reconciliation_grace_seconds,
        ),
        active_requests,
    )
    maintenance = AdminMaintenance(ledger)
    upstream = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock, ledger))
    downstream = DownstreamTokenRepository(
        DownstreamTokenDependencies(sessions, clock, SystemTokenHexSource(), ledger)
    )
    repositories = CredentialRepositories(upstream, downstream, sessions, clock, ledger)
    attempts = AttemptRepository(
        AttemptRepositoryDependencies(sessions, vault, clock, fail_stop, ledger)
    )
    logger = init_logging(
        LoggingConfig(
            name="nvidia-build-lb",
            stage=settings.stage,
            level=settings.log_level,
            stream=sys.stdout,
        )
    )
    client: SanitizedAsyncClient | None = None
    epoch: service_epoch.ServiceEpochRuntime | None = None
    maintenance_scope: anyio.CancelScope | None = None
    maintenance_joined: anyio.Event | None = None
    try:
        _ = await ensure_vault_key_binding(sessions, vault)
        client = SanitizedAsyncClient.create()
        adapter = NvidiaHostedAdapter(
            NvidiaAdapterDependencies(
                client=client,
                monotonic_clock=clock,
                wall_clock=clock,
                sleeper=AnyioPollingSleeper(),
                jitter=jitter,
                fail_stop=fail_stop,
            )
        )
        epoch = await service_epoch.ServiceEpochCoordinator(
            service_epoch.ServiceEpochDependencies(
                connection_factory=PsycopgEpochConnectionFactory(settings.database_url),
                pin_cleaner=attempts,
                uuid_source=uuids,
                publisher=EpochPublisher(),
                readiness=readiness,
                pre_publish=AdminMaintenancePrePublish(maintenance),
            )
        ).start(tasks, LifecycleMonitorSubmitter(readiness, fail_stop))
        maintenance_scope, maintenance_joined = await start_maintenance_worker(
            tasks,
            maintenance,
            settings.admin_ledger_maintenance_interval_seconds,
            logger,
        )
        routing = RoutingCoordinator(
            RoutingCoordinatorDependencies(
                attempts=attempts,
                adapter=adapter,
                clock=clock,
                monotonic_clock=clock,
                uuid_source=uuids,
                service_epoch=epoch.epoch,
                jitter=jitter,
                fail_stop=fail_stop,
            )
        )
        responders = LoggedResponderFactory(attempts, clock, clock, logger)
        gated = GatedCredentialRepositories(repositories, readiness, mutation_barrier)
        credentials = CredentialServices(
            gated,
            CredentialAuthenticators(
                AdminAuthenticator(settings.admin_token),
                DownstreamAuthenticator(downstream),
            ),
            RoutingProbeExecutor(routing, upstream, responders),
            lifecycle=readiness,
            mutation_barrier=mutation_barrier,
            admin_read_deadline_seconds=settings.admin_read_deadline_seconds,
            admin_mutation_deadline_seconds=settings.admin_mutation_deadline_seconds,
        )
        services = ApplicationServices(
            credentials=credentials,
            routing=routing,
            responders=responders,
            readiness=LifecycleReadinessProbe(
                readiness,
                RepositoryReadinessProbe(gated, settings.admin_read_deadline_seconds),
            ),
            logger=logger,
            active_requests=active_requests,
        )
        application = create_app(services)
        logger.emit(SafeLogEvent(name=LogEventName.SERVICE_STARTED, level=LogLevel.INFO))
        return ProductionResources(
            application,
            engine,
            client,
            epoch,
            readiness,
            logger,
            maintenance_scope,
            maintenance_joined,
        )
    except BaseException:
        _ = await close_production_resources(
            ProductionCleanup(
                readiness,
                engine,
                client,
                epoch,
                maintenance_scope,
                maintenance_joined,
                _MAINTENANCE_JOIN_TIMEOUT_SECONDS,
            )
        )
        raise
