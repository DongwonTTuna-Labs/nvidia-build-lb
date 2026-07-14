"""Per-key cooldown calculations from persisted streak snapshots."""

from datetime import datetime, timedelta
from typing import Final

from nvidia_build_lb.attempt_types import AttemptLease, CooldownKind
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.outcomes import RoutingTransition
from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.routing_models import CooldownJitter, MonotonicClock

_RATE_CAPS: Final = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
_TRANSIENT_CAPS: Final = (1.0, 2.0, 4.0, 8.0, 15.0)


def cooldown_for_failure(
    failure: FailureTerminal,
    lease: AttemptLease,
    clock: Clock,
    jitter: CooldownJitter,
) -> tuple[datetime | None, CooldownKind | None]:
    """Return one atomic cooldown pair using the lease-time streak snapshot."""
    transition = failure.outcome.transition
    if transition is RoutingTransition.RATE_COOLDOWN:
        seconds = failure.retry_after_seconds
        if seconds is None:
            cap = _RATE_CAPS[min(max(lease.consecutive_rate_limits, 0), len(_RATE_CAPS) - 1)]
            seconds = jitter.uniform(cap / 2, cap)
        return clock.now() + timedelta(seconds=seconds), CooldownKind.RATE_LIMIT
    if transition is RoutingTransition.TRANSIENT_COOLDOWN:
        cap = _TRANSIENT_CAPS[
            min(
                max(lease.consecutive_transient_failures, 0),
                len(_TRANSIENT_CAPS) - 1,
            )
        ]
        seconds = jitter.uniform(cap / 2, cap)
        return clock.now() + timedelta(seconds=seconds), CooldownKind.TRANSIENT
    return None, None


def latency_ms(clock: MonotonicClock, started: float) -> int:
    """Return a nonnegative integer latency observation."""
    return max(0, int((clock.monotonic() - started) * 1000))
