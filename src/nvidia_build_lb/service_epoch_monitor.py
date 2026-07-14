"""Typed same-connection service epoch monitoring with clean cancellation."""

import threading
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import ClassVar, Protocol, final
from uuid import UUID

import anyio

from nvidia_build_lb.service_epoch_types import EpochConnectionUnexpectedCloseError

MONITOR_INTERVAL_SECONDS = 1
MONITOR_TIMEOUT_SECONDS = 2
MONITOR_SUBMIT_TIMEOUT_SECONDS = 2


@unique
class MonitorFatalClass(StrEnum):
    """Secret-free connection-monitor failure classes."""

    TIMEOUT = "timeout"
    SQL_ERROR = "sql_error"
    UNEXPECTED_EOF = "unexpected_eof"
    UNEXPECTED_CLOSE = "unexpected_close"


@dataclass(frozen=True, slots=True)
class MonitorHealthy:
    """One exact liveness query succeeded."""


@dataclass(frozen=True, slots=True)
class MonitorFatal:
    """One safe monitor failure observation."""

    failure_class: MonitorFatalClass


@dataclass(frozen=True, slots=True)
class MonitorCancelled:
    """The monitor unwound with or without an exact clean-stop token."""

    clean_token: UUID | None


type MonitorEvent = MonitorHealthy | MonitorFatal | MonitorCancelled


class MonitorConnection(Protocol):
    """Narrow same-connection liveness surface."""

    async def ping(self) -> bool:
        """Run the exact liveness query."""
        ...


class MonitorSubmitter(Protocol):
    """Submit typed observations to the lifecycle coordinator."""

    async def submit(self, event: MonitorEvent) -> None:
        """Submit one bounded typed event."""
        ...


class Sleeper(Protocol):
    """Await monitor intervals through an injected clock."""

    async def sleep(self, seconds: float) -> None:
        """Sleep for fixed nonnegative seconds."""
        ...


@final
class CleanStopState:
    """Publish one exact token immediately before clean monitor cancellation."""

    __slots__: ClassVar[tuple[str, ...]] = ("_lock", "_token")
    _lock: threading.Lock
    _token: UUID | None

    def __init__(self) -> None:
        """Initialize one unset token behind a process-local lock."""
        self._lock = threading.Lock()
        self._token = None

    def request(self, token: UUID) -> None:
        """Set the one clean-stop token once."""
        with self._lock:
            if self._token is not None:
                raise RuntimeError
            self._token = token

    def current(self) -> UUID | None:
        """Read the current clean-stop token."""
        with self._lock:
            return self._token


@final
class MonitorFatalLatch:
    """Accept the first fatal monitor observation and reject every later one."""

    __slots__: ClassVar[tuple[str, ...]] = ("_fatal", "_lock")
    _fatal: MonitorFatal | None
    _lock: threading.Lock

    def __init__(self) -> None:
        """Initialize one open one-shot CAS."""
        self._fatal = None
        self._lock = threading.Lock()

    def register(self, fatal: MonitorFatal) -> bool:
        """Return true only for the first accepted fatal event."""
        with self._lock:
            if self._fatal is not None:
                return False
            self._fatal = fatal
            return True

    def current(self) -> MonitorFatal | None:
        """Return the accepted fatal observation, if any."""
        with self._lock:
            return self._fatal


@dataclass(frozen=True, slots=True)
class AnyioSleeper:
    """Default AnyIO monitor sleeper."""

    async def sleep(self, seconds: float) -> None:
        """Sleep without blocking the event loop."""
        await anyio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class ServiceEpochMonitor:
    """Observe only; never mutate readiness, lifecycle, or connection ownership."""

    connection: MonitorConnection
    submitter: MonitorSubmitter
    sleeper: Sleeper
    clean_stop: CleanStopState

    async def run(self) -> None:
        """Submit health until one fatal observation or cancellation."""
        cancelled_type = anyio.get_cancelled_exc_class()
        try:
            while True:
                await self.sleeper.sleep(MONITOR_INTERVAL_SECONDS)
                healthy = False
                fatal: MonitorFatal | None = None
                try:
                    with anyio.fail_after(MONITOR_TIMEOUT_SECONDS):
                        healthy = await self.connection.ping()
                except TimeoutError:
                    fatal = MonitorFatal(MonitorFatalClass.TIMEOUT)
                except EpochConnectionUnexpectedCloseError:
                    fatal = MonitorFatal(MonitorFatalClass.UNEXPECTED_CLOSE)
                except Exception:  # noqa: BLE001 - SQL exception text is never retained.
                    fatal = MonitorFatal(MonitorFatalClass.SQL_ERROR)
                if fatal is not None:
                    await _submit_bounded(self.submitter, fatal)
                    return
                if not healthy:
                    await _submit_bounded(
                        self.submitter,
                        MonitorFatal(MonitorFatalClass.UNEXPECTED_EOF),
                    )
                    return
                await _submit_bounded(self.submitter, MonitorHealthy())
        except cancelled_type as error:
            cancellation = error
        with anyio.CancelScope(shield=True):
            await _submit_bounded(
                self.submitter,
                MonitorCancelled(self.clean_stop.current()),
            )
        raise cancellation


async def _submit_bounded(submitter: MonitorSubmitter, event: MonitorEvent) -> None:
    timed_out = False
    try:
        with anyio.fail_after(MONITOR_SUBMIT_TIMEOUT_SECONDS):
            await submitter.submit(event)
    except TimeoutError:
        timed_out = True
    if timed_out:
        raise TimeoutError
