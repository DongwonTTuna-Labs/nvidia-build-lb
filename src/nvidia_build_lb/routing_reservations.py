"""Safe public failures for routing reservation outcomes."""

from nvidia_build_lb.outcomes import (
    HttpStatusSignal,
    NoEligibleKey,
    ReservationFailure,
    map_public_outcome,
)
from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.scheduler_state import NoEligibleUpstreamKeyError
from nvidia_build_lb.scheduler_types import NoEligibleReason


def reservation_failure() -> FailureTerminal:
    """Map an unproven local reservation to the closed database failure."""
    return FailureTerminal(map_public_outcome(ReservationFailure()), None)


def no_eligible_failure(error: NoEligibleUpstreamKeyError) -> FailureTerminal:
    """Map the locked no-eligible snapshot without enabling an alternate."""
    if error.reason is NoEligibleReason.RATE_COOLDOWN:
        outcome = map_public_outcome(HttpStatusSignal(429)).without_alternate()
        return FailureTerminal(outcome, error.retry_after_seconds)
    if error.reason is NoEligibleReason.TRANSIENT_COOLDOWN:
        outcome = map_public_outcome(HttpStatusSignal(503)).without_alternate()
        return FailureTerminal(outcome, None)
    return FailureTerminal(map_public_outcome(NoEligibleKey()), None)
