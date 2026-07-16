"""Bounded maintenance and resource retirement for the production graph."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import anyio
from anyio.abc import TaskGroup, TaskStatus
from sqlalchemy.ext.asyncio import AsyncEngine

from nvidia_build_lb.admin_maintenance import AdminMaintenance, AdminMaintenanceWorker
from nvidia_build_lb.config import LogLevel
from nvidia_build_lb.logging import LogEventName, SafeLogEvent, ServiceLogger
from nvidia_build_lb.runtime_primitives import SystemUuidSource
from nvidia_build_lb.runtime_readiness import RuntimeReadinessGate
from nvidia_build_lb.service_epoch import (
    ADVISORY_LOCK_KEY_ONE,
    ADVISORY_LOCK_KEY_TWO,
    MONITOR_JOIN_TIMEOUT_SECONDS,
    ServiceEpochRuntime,
)
from nvidia_build_lb.transport_adapter import SanitizedAsyncClient

_CLEANUP_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class EpochPublisher:
    """Retain the service epoch published during production graph startup."""

    epoch: UUID | None = None

    def publish(self, epoch: UUID) -> None:
        """Record the published epoch without additional side effects."""
        self.epoch = epoch


@dataclass(frozen=True, slots=True)
class ProductionCleanup:
    """Every optional startup resource owned by one cleanup boundary."""

    readiness: RuntimeReadinessGate
    engine: AsyncEngine
    client: SanitizedAsyncClient | None
    epoch: ServiceEpochRuntime | None
    maintenance_scope: anyio.CancelScope | None
    maintenance_joined: anyio.Event | None
    maintenance_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class _MaintenanceFailureReporter:
    logger: ServiceLogger

    def __call__(self) -> None:
        self.logger.emit(
            SafeLogEvent(
                name=LogEventName.MAINTENANCE_FAILED,
                level=LogLevel.ERROR,
            )
        )


async def start_maintenance_worker(
    tasks: TaskGroup,
    maintenance: AdminMaintenance,
    interval_seconds: int,
    logger: ServiceLogger,
) -> tuple[anyio.CancelScope, anyio.Event]:
    """Start the recurring ledger worker and return its cancellation receipt."""
    scope = anyio.CancelScope()
    joined = anyio.Event()
    worker = AdminMaintenanceWorker(
        maintenance,
        interval_seconds,
        _MaintenanceFailureReporter(logger),
    )
    await tasks.start(_run_maintenance, worker, scope, joined)
    return scope, joined


async def _run_maintenance(
    worker: AdminMaintenanceWorker,
    scope: anyio.CancelScope,
    joined: anyio.Event,
    *,
    task_status: TaskStatus[None],
) -> None:
    try:
        with scope:
            task_status.started()
            await worker.run()
    finally:
        joined.set()


async def close_epoch(runtime: ServiceEpochRuntime) -> bool:
    """Request a clean stop and release the epoch connection within bounds."""
    try:
        runtime.clean_stop.request(SystemUuidSource().new())
    except RuntimeError:
        return False
    runtime.monitor_scope.cancel()
    joined = await bounded(runtime.monitor_joined.wait, MONITOR_JOIN_TIMEOUT_SECONDS)
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
    closed = await bounded(runtime.connection.close)
    return joined and unlocked and closed


async def close_production_resources(resources: ProductionCleanup) -> bool:
    """Retire every resource started so far in one fixed bounded order."""
    resources.readiness.set_ready(False)
    maintenance_closed = True
    if resources.maintenance_scope is not None:
        resources.maintenance_scope.cancel()
        maintenance_closed = resources.maintenance_joined is not None and await bounded(
            resources.maintenance_joined.wait,
            resources.maintenance_timeout_seconds,
        )
    elif resources.maintenance_joined is not None:
        maintenance_closed = False
    epoch_closed = resources.epoch is None or await close_epoch(resources.epoch)
    client_closed = resources.client is None or await bounded(resources.client.aclose)
    engine_closed = maintenance_closed and await bounded(resources.engine.dispose)
    return maintenance_closed and epoch_closed and client_closed and engine_closed


async def bounded[T](
    operation: Callable[[], Awaitable[T]],
    budget_seconds: float = _CLEANUP_TIMEOUT_SECONDS,
) -> bool:
    """Run one cleanup operation in a hard, cancellation-shielded budget."""
    try:
        with anyio.CancelScope(shield=True):
            with anyio.fail_after(budget_seconds):
                _ = await operation()
    except (Exception, anyio.get_cancelled_exc_class()):  # noqa: BLE001
        return False
    return True
