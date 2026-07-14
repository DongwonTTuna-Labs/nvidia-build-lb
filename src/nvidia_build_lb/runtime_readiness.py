"""One process-lifecycle readiness gate shared by HTTP and fail-stop paths."""

import threading
from dataclasses import dataclass, field
from typing import Protocol

from nvidia_build_lb.admin.schemas import AdminEventListResponse, AdminOverviewRead, OverviewStatus
from nvidia_build_lb.attempt_fail_stop import AttemptFailStop, ReadinessGate
from nvidia_build_lb.credential_protocols import (
    CredentialRepositorySurface,
    DownstreamCredentialRepository,
    UpstreamCredentialRepository,
)
from nvidia_build_lb.service_epoch_monitor import (
    AnyioSleeper,
    MonitorCancelled,
    MonitorEvent,
    MonitorFatal,
    MonitorFatalClass,
    MonitorFatalLatch,
    MonitorHealthy,
    Sleeper,
)

MONITOR_FATAL_GRACE_SECONDS = 2.0


class _ReadinessProbe(Protocol):
    async def is_ready(self) -> bool:
        """Return current application readiness."""
        ...


@dataclass(slots=True)
class RuntimeReadinessGate:
    """Publish one thread-safe lifecycle readiness bit, closed by default."""

    _ready: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def set_ready(self, ready: bool) -> None:
        """Set lifecycle readiness synchronously for fail-stop ordering."""
        with self._lock:
            self._ready = ready

    def is_ready(self) -> bool:
        """Return the current lifecycle readiness bit."""
        with self._lock:
            return self._ready


@dataclass(frozen=True, slots=True)
class LifecycleReadinessProbe:
    """Short-circuit health after lifecycle withdrawal without database I/O."""

    lifecycle: RuntimeReadinessGate
    inner: _ReadinessProbe

    async def is_ready(self) -> bool:
        """Return false immediately after fail-stop readiness withdrawal."""
        if not self.lifecycle.is_ready():
            return False
        return await self.inner.is_ready()


@dataclass(frozen=True, slots=True)
class GatedCredentialRepositories:
    """Apply lifecycle readiness to the same overview used by health and admin."""

    inner: CredentialRepositorySurface
    lifecycle: RuntimeReadinessGate

    @property
    def upstream(self) -> UpstreamCredentialRepository:
        """Return the unchanged upstream administration repository."""
        return self.inner.upstream

    @property
    def downstream(self) -> DownstreamCredentialRepository:
        """Return the unchanged downstream administration repository."""
        return self.inner.downstream

    async def overview(self) -> AdminOverviewRead:
        """Return ready only when both lifecycle and repository state are ready."""
        overview = await self.inner.overview()
        if self.lifecycle.is_ready() and overview.ready:
            return overview
        return overview.model_copy(update={"status": OverviewStatus.DEGRADED, "ready": False})

    async def events(self) -> AdminEventListResponse:
        """Return the unchanged safe event collection."""
        return await self.inner.events()


@dataclass(frozen=True, slots=True)
class LifecycleMonitorSubmitter:
    """Turn the first fatal or unclean monitor cancellation into fail-stop."""

    readiness: ReadinessGate
    fail_stop: AttemptFailStop
    sleeper: Sleeper = field(default_factory=AnyioSleeper)
    fatal_latch: MonitorFatalLatch = field(default_factory=MonitorFatalLatch)

    async def submit(self, event: MonitorEvent) -> None:
        """Accept healthy/clean events and fail-stop once for fatal observations."""
        fatal: MonitorFatal | None = None
        if isinstance(event, MonitorFatal):
            fatal = event
        elif isinstance(event, MonitorCancelled) and event.clean_token is None:
            fatal = MonitorFatal(MonitorFatalClass.UNEXPECTED_CLOSE)
        elif isinstance(event, MonitorHealthy):
            return
        if fatal is not None and self.fatal_latch.register(fatal):
            try:
                self.readiness.set_ready(False)
                await self.sleeper.sleep(MONITOR_FATAL_GRACE_SECONDS)
            finally:
                self.fail_stop.trigger()
