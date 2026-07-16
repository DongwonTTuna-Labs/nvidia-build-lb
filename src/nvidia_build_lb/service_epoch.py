"""Single-process service epoch startup and typed connection monitoring."""

from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, override
from uuid import UUID, uuid4

import anyio
from anyio.abc import TaskGroup

from nvidia_build_lb.service_epoch_cleanup import (
    CleanupDependencies,
    CleanupResult,
)
from nvidia_build_lb.service_epoch_connection_validation import is_valid_epoch_connection
from nvidia_build_lb.service_epoch_monitor import (
    AnyioSleeper,
    CleanStopState,
    MonitorSubmitter,
    ServiceEpochMonitor,
    Sleeper,
)
from nvidia_build_lb.service_epoch_startup_cleanup import (
    cleanup_failed_start,
    close_invalid_connection,
    stop_startup_monitor,
)
from nvidia_build_lb.service_epoch_supervision import supervise_epoch_cleanup
from nvidia_build_lb.service_epoch_types import (
    EpochConnectionFactory,
    EpochSqlConnection,
    PriorEpochCleanup,
)

ADVISORY_LOCK_KEY_ONE = 1_312_967_746
ADVISORY_LOCK_KEY_TWO = 1
_UUID_VERSION = 4
MONITOR_JOIN_TIMEOUT_SECONDS = 5.0
STARTUP_CLEANUP_TIMEOUT_SECONDS = 5.0


class ServiceEpochLockUnavailableError(Exception):
    """The singleton advisory lock was not acquired exactly once."""

    @override
    def __str__(self) -> str:
        return "service_epoch_lock_unavailable"


class InvalidServiceEpochError(Exception):
    """The injected UUID source did not produce UUIDv4."""

    @override
    def __str__(self) -> str:
        return "invalid_service_epoch"


class PinCleaner(Protocol):
    """Delete only prior-epoch pins on the supplied locked connection."""

    async def cleanup_prior_epoch_pins(
        self,
        connection: EpochSqlConnection,
        active_epoch: UUID,
    ) -> PriorEpochCleanup:
        """Commit one cleanup transaction."""
        ...


class AdvisoryLockConnection(Protocol):
    """Narrow one-call startup advisory-lock surface."""

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool | None:
        """Attempt the two-key session lock once."""
        ...


class UuidSource(Protocol):
    """Create one process-local service epoch UUID."""

    def new(self) -> UUID:
        """Return one UUIDv4."""
        ...


class EpochPublisher(Protocol):
    """Publish the epoch to request construction only after cleanup commit."""

    def publish(self, epoch: UUID) -> None:
        """Publish one immutable epoch."""
        ...


class PrePublishHook(Protocol):
    """Run required startup maintenance before epoch publication."""

    async def __call__(self, epoch: UUID) -> None:
        """Complete one startup pass or fail startup."""
        ...


class ReadinessGate(Protocol):
    """Control intake readiness."""

    def set_ready(self, ready: bool) -> None:
        """Set current readiness."""
        ...


@dataclass(frozen=True, slots=True)
class Uuid4Source:
    """Default cryptographic UUIDv4 source."""

    def new(self) -> UUID:
        """Return one fresh UUIDv4."""
        return uuid4()


@dataclass(frozen=True, slots=True)
class ServiceEpochDependencies:
    """All startup I/O and state publication dependencies."""

    connection_factory: EpochConnectionFactory
    pin_cleaner: PinCleaner
    uuid_source: UuidSource
    publisher: EpochPublisher
    readiness: ReadinessGate
    pre_publish: PrePublishHook
    sleeper: Sleeper = field(default_factory=AnyioSleeper)


@dataclass(frozen=True, slots=True)
class ServiceEpochRuntime:
    """Owned live epoch resources returned only after readiness publication."""

    epoch: UUID
    connection: EpochSqlConnection
    clean_stop: CleanStopState
    monitor_scope: anyio.CancelScope
    monitor_joined: anyio.Event


@dataclass(frozen=True, slots=True)
class ServiceEpochCoordinator:
    """Own ordered startup and hand live resources to the lifecycle owner."""

    dependencies: ServiceEpochDependencies

    async def open(self) -> EpochSqlConnection:
        """Open one dedicated autocommit psycopg connection."""
        connection = await self.dependencies.connection_factory.open()
        if not is_valid_epoch_connection(connection):
            await close_invalid_connection(
                connection,
                timeout_seconds=STARTUP_CLEANUP_TIMEOUT_SECONDS,
            )
            raise ServiceEpochLockUnavailableError
        return connection

    @staticmethod
    async def acquire_lock(connection: AdvisoryLockConnection) -> None:
        """Call pg_try_advisory_lock exactly once and require true."""
        acquired = await connection.try_advisory_lock(
            ADVISORY_LOCK_KEY_ONE,
            ADVISORY_LOCK_KEY_TWO,
        )
        if acquired is not True:
            raise ServiceEpochLockUnavailableError

    def start_epoch(self) -> UUID:
        """Create exactly one UUIDv4 for this process startup."""
        epoch = self.dependencies.uuid_source.new()
        if epoch.version != _UUID_VERSION:
            raise InvalidServiceEpochError
        return epoch

    async def start(
        self,
        tasks: TaskGroup,
        submitter: MonitorSubmitter,
    ) -> ServiceEpochRuntime:
        """Clean prior pins and publish ready in the locked startup order."""
        connection = await self.open()
        locked = False
        monitor_started = False
        scope: anyio.CancelScope | None = None
        joined: anyio.Event | None = None
        try:
            await self.acquire_lock(connection)
            locked = True
            epoch = self.start_epoch()
            _ = await self.dependencies.pin_cleaner.cleanup_prior_epoch_pins(connection, epoch)
            await self.dependencies.pre_publish(epoch)
            self.dependencies.publisher.publish(epoch)
            clean_stop = CleanStopState()
            scope = anyio.CancelScope()
            joined = anyio.Event()
            monitor = ServiceEpochMonitor(
                connection,
                submitter,
                self.dependencies.sleeper,
                clean_stop,
            )
            _ = tasks.start_soon(_run_monitor, monitor, scope, joined)
            monitor_started = True
            self.dependencies.readiness.set_ready(True)
            return ServiceEpochRuntime(epoch, connection, clean_stop, scope, joined)
        except BaseException:
            with suppress(BaseException):
                self.dependencies.readiness.set_ready(False)
            if monitor_started and scope is not None and joined is not None:
                await stop_startup_monitor(
                    scope,
                    joined,
                    timeout_seconds=MONITOR_JOIN_TIMEOUT_SECONDS,
                )
            await cleanup_failed_start(
                connection,
                locked=locked,
                first_lock_key=ADVISORY_LOCK_KEY_ONE,
                second_lock_key=ADVISORY_LOCK_KEY_TWO,
                timeout_seconds=STARTUP_CLEANUP_TIMEOUT_SECONDS,
            )
            raise


async def _run_monitor(
    monitor: ServiceEpochMonitor,
    scope: anyio.CancelScope,
    joined: anyio.Event,
) -> None:
    try:
        with scope:
            await monitor.run()
    finally:
        joined.set()


async def run_supervised_epoch_subprocess(
    runtime: ServiceEpochRuntime,
    dependencies: CleanupDependencies,
) -> CleanupResult:
    """Clean-cancel and join the monitor before the final DB/unlock/close sequence."""
    return await supervise_epoch_cleanup(
        runtime,
        dependencies,
        join_timeout_seconds=MONITOR_JOIN_TIMEOUT_SECONDS,
    )
