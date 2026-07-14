"""Small production clock, entropy, sleep, and cancellation implementations."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from random import SystemRandom
from time import monotonic
from uuid import UUID, uuid4

import anyio


@dataclass(frozen=True, slots=True)
class SystemClock:
    """Provide aware wall time and monotonic process time."""

    def now(self) -> datetime:
        """Return current UTC wall time."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return the process monotonic clock."""
        return monotonic()


@dataclass(frozen=True, slots=True)
class SystemUuidSource:
    """Create cryptographic UUIDv4 identities."""

    def new(self) -> UUID:
        """Return one fresh UUIDv4."""
        return uuid4()


@dataclass(frozen=True, slots=True)
class SystemJitter:
    """Choose cooldown and poll jitter with the OS-backed random source."""

    _random: SystemRandom = field(default_factory=SystemRandom, repr=False)

    def uniform(self, lower: float, upper: float) -> float:
        """Return one random value in the inclusive interval."""
        return self._random.uniform(lower, upper)


@dataclass(frozen=True, slots=True)
class AnyioPollingSleeper:
    """Sleep without blocking the event loop."""

    async def sleep(self, seconds: float) -> None:
        """Await the requested nonnegative duration."""
        await anyio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class CancelScopeRootCancellation:
    """Expose one entered AnyIO scope through the fail-stop protocol."""

    scope: anyio.CancelScope

    def cancel(self) -> None:
        """Cancel the request-serving root scope."""
        self.scope.cancel()
