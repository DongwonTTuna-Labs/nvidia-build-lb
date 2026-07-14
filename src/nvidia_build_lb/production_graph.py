"""Construct and retire the complete database-backed production service graph."""

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import anyio
from anyio.abc import TaskGroup
from sqlalchemy.ext.asyncio import AsyncEngine

from nvidia_build_lb.admin_credentials import CredentialRepositories, CredentialServices
from nvidia_build_lb.api_streaming import LoggedResponderFactory
from nvidia_build_lb.api_types import ApplicationServices, RepositoryReadinessProbe
from nvidia_build_lb.attempt_fail_stop import LifecycleAttemptFailStop
from nvidia_build_lb.attempt_repository import AttemptRepository, AttemptRepositoryDependencies
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
from nvidia_build_lb.service_epoch import (
    ADVISORY_LOCK_KEY_ONE,
    ADVISORY_LOCK_KEY_TWO,
    MONITOR_JOIN_TIMEOUT_SECONDS,
    ServiceEpochCoordinator,
    ServiceEpochDependencies,
    ServiceEpochRuntime,
)
from nvidia_build_lb.service_epoch_connection import PsycopgEpochConnectionFactory
from nvidia_build_lb.transport_adapter import SanitizedAsyncClient
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault
from nvidia_build_lb.vault_key_binding import ensure_vault_key_binding

_CLEANUP_TIMEOUT_SECONDS = 5.0
_PRODUCTION_CLEANUP_FAILED = "production_cleanup_failed"


@dataclass(frozen=True, slots=True)
class ProductionResources:
    """All application-lifetime resources owned by the process entrypoint."""

    app: SafeServerErrorFastAPI
    engine: AsyncEngine
    client: SanitizedAsyncClient
    epoch: ServiceEpochRuntime
    readiness: RuntimeReadinessGate
    logger: ServiceLogger

    async def close(self) -> None:
        """Withdraw readiness and close every owned resource with bounded cleanup."""
        self.readiness.set_ready(False)
        failed = not await _close_epoch(self.epoch)
        failed = not await _bounded(self.client.aclose) or failed
        failed = not await _bounded(self.engine.dispose) or failed
        if failed:
            raise RuntimeError(_PRODUCTION_CLEANUP_FAILED)


async def build_production_resources(
    settings: Settings,
    tasks: TaskGroup,
    root_scope: anyio.CancelScope,
) -> ProductionResources:
    """Build one real repository, epoch, NVIDIA, auth, and HTTP graph."""
    clock = SystemClock()
    jitter = SystemJitter()
    uuids = SystemUuidSource()
    readiness = RuntimeReadinessGate()
    fail_stop = LifecycleAttemptFailStop(
        readiness,
        CancelScopeRootCancellation(root_scope),
    )
    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    vault = Vault(settings.vault_key)
    upstream = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock))
    downstream = DownstreamTokenRepository(
        DownstreamTokenDependencies(sessions, clock, SystemTokenHexSource())
    )
    repositories = CredentialRepositories(upstream, downstream, sessions, clock)
    attempts = AttemptRepository(AttemptRepositoryDependencies(sessions, vault, clock, fail_stop))
    logger = init_logging(
        LoggingConfig(
            name="nvidia-build-lb",
            stage=settings.stage,
            level=settings.log_level,
            stream=sys.stdout,
        )
    )
    client: SanitizedAsyncClient | None = None
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
        epoch = await ServiceEpochCoordinator(
            ServiceEpochDependencies(
                connection_factory=PsycopgEpochConnectionFactory(settings.database_url),
                pin_cleaner=attempts,
                uuid_source=uuids,
                publisher=_EpochPublisher(),
                readiness=readiness,
            )
        ).start(tasks, LifecycleMonitorSubmitter(readiness, fail_stop))
    except BaseException:
        if client is not None:
            _ = await _bounded(client.aclose)
        _ = await _bounded(engine.dispose)
        raise
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
    gated = GatedCredentialRepositories(repositories, readiness)
    credentials = CredentialServices(
        gated,
        CredentialAuthenticators(
            AdminAuthenticator(settings.admin_token),
            DownstreamAuthenticator(downstream),
        ),
        RoutingProbeExecutor(routing, upstream, responders),
    )
    services = ApplicationServices(
        credentials=credentials,
        routing=routing,
        responders=responders,
        readiness=LifecycleReadinessProbe(readiness, RepositoryReadinessProbe(gated)),
        logger=logger,
    )
    application = create_app(services)
    logger.emit(SafeLogEvent(name=LogEventName.SERVICE_STARTED, level=LogLevel.INFO))
    return ProductionResources(application, engine, client, epoch, readiness, logger)


@dataclass(slots=True)
class _EpochPublisher:
    epoch: UUID | None = None

    def publish(self, epoch: UUID) -> None:
        self.epoch = epoch


async def _close_epoch(runtime: ServiceEpochRuntime) -> bool:
    try:
        runtime.clean_stop.request(SystemUuidSource().new())
    except RuntimeError:
        return False
    runtime.monitor_scope.cancel()
    joined = await _bounded(runtime.monitor_joined.wait, MONITOR_JOIN_TIMEOUT_SECONDS)
    unlocked = False
    if joined:
        try:
            with anyio.CancelScope(shield=True):
                with anyio.fail_after(_CLEANUP_TIMEOUT_SECONDS):
                    unlocked = (
                        await runtime.connection.advisory_unlock(
                            ADVISORY_LOCK_KEY_ONE,
                            ADVISORY_LOCK_KEY_TWO,
                        )
                        is True
                    )
        except (Exception, anyio.get_cancelled_exc_class()):  # noqa: BLE001
            unlocked = False
    closed = await _bounded(runtime.connection.close)
    return joined and unlocked and closed


async def _bounded[T](
    operation: Callable[[], Awaitable[T]],
    budget_seconds: float = _CLEANUP_TIMEOUT_SECONDS,
) -> bool:
    try:
        with anyio.CancelScope(shield=True):
            with anyio.fail_after(budget_seconds):
                _ = await operation()
    except (Exception, anyio.get_cancelled_exc_class()):  # noqa: BLE001
        return False
    return True
