"""Typed dependencies and results for sequential NVIDIA routing."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from pydantic import SecretStr

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptLease,
    AttemptStartCommand,
    TerminalCommitted,
)
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.polling import FailureTerminal, JsonTerminal, PollTerminal, StreamTerminal
from nvidia_build_lb.scheduler_state import TerminalOutcome


class AttemptStore(Protocol):
    """Durable reservation and terminal operations used by routing."""

    async def reserve_attempt(self, command: AttemptStartCommand) -> AttemptLease:
        """Reserve one exact attempt."""
        ...

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        """Commit one exact terminal observation."""
        ...


class HostedAdapter(Protocol):
    """Execute one origin request and any same-key 202 polling."""

    async def execute(self, *, credential: SecretStr, body: bytes) -> PollTerminal:
        """Return one strict terminal result."""
        ...


class MonotonicClock(Protocol):
    """Supply monotonic time for integer latency."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""
        ...


class UuidSource(Protocol):
    """Preallocate stable start and terminal IDs."""

    def new(self) -> UUID:
        """Return one fresh UUID."""
        ...


class CooldownJitter(Protocol):
    """Choose equal jitter inside one half-to-cap interval."""

    def uniform(self, lower: float, upper: float) -> float:
        """Return one bounded delay."""
        ...


@dataclass(frozen=True, slots=True)
class RoutingCoordinatorDependencies:
    """Persistence, adapter, time, IDs, epoch, and jitter dependencies."""

    attempts: AttemptStore
    adapter: HostedAdapter
    clock: Clock
    monotonic_clock: MonotonicClock
    uuid_source: UuidSource
    service_epoch: UUID
    jitter: CooldownJitter
    fail_stop: AttemptFailStop


@dataclass(frozen=True, slots=True)
class RoutedAttemptObservation:
    """Safe identity and terminal fields for one durably completed attempt."""

    key_id: UUID
    attempt_ordinal: int
    safe_status_class: LastStatusClass
    terminal_outcome: TerminalOutcome

    def __post_init__(self) -> None:
        """Reject observations that cannot identify a real routed attempt."""
        if self.attempt_ordinal < 1:
            raise ValueError


@dataclass(frozen=True, slots=True)
class RoutedJson:
    """A successful JSON result whose terminal receipt is already committed."""

    terminal: JsonTerminal
    key_id: UUID
    attempt_count: int
    stream_frames: tuple[bytes, bytes] | None = None
    attempt_observations: tuple[RoutedAttemptObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutedStream:
    """A live stream and lease handed to terminal arbitration without early success."""

    terminal: StreamTerminal
    lease: AttemptLease
    attempt_count: int
    started_monotonic: float
    attempt_observations: tuple[RoutedAttemptObservation, ...] = ()


@dataclass(frozen=True, slots=True)
class RoutedFailure:
    """The precedence-selected failure after zero, one, or two attempts."""

    terminal: FailureTerminal
    attempt_count: int
    attempt_observations: tuple[RoutedAttemptObservation, ...] = ()


type RoutedResult = RoutedJson | RoutedStream | RoutedFailure
