"""Safe public failures for routing reservation outcomes."""

from uuid import UUID

import anyio

from nvidia_build_lb.admin_ledger import LedgerCapacityExhaustedError
from nvidia_build_lb.attempt_types import AttemptLease, AttemptStartCommand
from nvidia_build_lb.outcomes import (
    HttpStatusSignal,
    LedgerCapacityExhausted,
    NoEligibleKey,
    ReservationFailure,
    map_public_outcome,
)
from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.routing_models import (
    RoutedFailure,
    RoutingCoordinatorDependencies,
)
from nvidia_build_lb.scheduler_state import NoEligibleUpstreamKeyError
from nvidia_build_lb.scheduler_types import NoEligibleReason, SchedulerStateUnavailableError
from nvidia_build_lb.vault import VaultDecryptionError


def build_start_command(
    dependencies: RoutingCoordinatorDependencies,
    request_id: str,
    explicit_probe_key_id: UUID | None,
    excluded_key_ids: frozenset[UUID],
) -> AttemptStartCommand:
    """Preallocate one durable reservation identity from coordinator dependencies."""
    return AttemptStartCommand(
        started_event_id=dependencies.uuid_source.new(),
        terminal_event_id=dependencies.uuid_source.new(),
        request_id=request_id,
        service_epoch=dependencies.service_epoch,
        explicit_probe_key_id=explicit_probe_key_id,
        excluded_key_ids=excluded_key_ids,
        started_at=dependencies.clock.now(),
    )


async def reserve_attempt(
    dependencies: RoutingCoordinatorDependencies,
    command: AttemptStartCommand,
    *,
    has_failure: bool,
    attempt_count: int,
) -> AttemptLease | RoutedFailure | None:
    """Map closed reservation outcomes without exposing persistence details."""
    try:
        with anyio.CancelScope(shield=True):
            return await dependencies.attempts.reserve_attempt(command)
    except NoEligibleUpstreamKeyError as error:
        if has_failure:
            return None
        return RoutedFailure(no_eligible_failure(error), 0)
    except LedgerCapacityExhaustedError:
        return RoutedFailure(
            FailureTerminal(map_public_outcome(LedgerCapacityExhausted()), None),
            attempt_count,
        )
    except (SchedulerStateUnavailableError, VaultDecryptionError):
        return RoutedFailure(reservation_failure(), attempt_count)


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
