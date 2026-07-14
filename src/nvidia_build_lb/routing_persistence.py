"""Cancellation-safe terminal persistence for pre-handoff routing attempts."""

from math import ceil

import anyio

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptLease,
    TerminalCommitted,
)
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.outcomes import RoutingTransition
from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.routing_cooldown import cooldown_for_failure, latency_ms
from nvidia_build_lb.routing_models import (
    AttemptStore,
    MonotonicClock,
    RoutingCoordinatorDependencies,
)
from nvidia_build_lb.scheduler_state import TerminalOutcome


async def finalize_shielded(
    attempts: AttemptStore,
    command: AttemptFinalizeCommand,
) -> TerminalCommitted:
    """Finish one bounded terminal commit despite ancestor cancellation."""
    committed: TerminalCommitted | None = None
    with anyio.CancelScope(shield=True):
        committed = await attempts.finalize_attempt(command)
    if committed is None:
        raise RuntimeError
    return committed


async def finalize_cancelled_shielded(
    attempts: AttemptStore,
    lease: AttemptLease,
    clock: Clock,
    monotonic_clock: MonotonicClock,
    started_monotonic: float,
) -> TerminalCommitted:
    """Persist the cancellation terminal before re-raising pre-handoff cancellation."""
    latency_ms = max(0, int((monotonic_clock.monotonic() - started_monotonic) * 1000))
    return await finalize_shielded(
        attempts,
        AttemptFinalizeCommand(
            identity=lease.identity,
            outcome=TerminalOutcome.CANCELLED,
            status_class=LastStatusClass.CANCELLED,
            latency_ms=latency_ms,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=clock.now(),
        ),
    )


async def finalize_success_shielded(
    dependencies: RoutingCoordinatorDependencies,
    lease: AttemptLease,
    started_monotonic: float,
) -> None:
    """Commit one successful pre-handoff attempt despite cancellation."""
    _ = await finalize_shielded(
        dependencies.attempts,
        AttemptFinalizeCommand.success(
            lease.identity,
            dependencies.clock.now(),
            latency_ms=latency_ms(dependencies.monotonic_clock, started_monotonic),
        ),
    )


async def finalize_failure_shielded(
    dependencies: RoutingCoordinatorDependencies,
    lease: AttemptLease,
    failure: FailureTerminal,
    started_monotonic: float,
) -> FailureTerminal:
    """Commit one failed pre-handoff attempt and expose its bounded cooldown."""
    status = failure.outcome.persisted_status or LastStatusClass.UPSTREAM_PROTOCOL_ERROR
    cooldown_until, cooldown_kind = cooldown_for_failure(
        failure,
        lease,
        dependencies.clock,
        dependencies.jitter,
    )
    _ = await finalize_shielded(
        dependencies.attempts,
        AttemptFinalizeCommand(
            identity=lease.identity,
            outcome=TerminalOutcome.FAILED,
            status_class=status,
            latency_ms=latency_ms(dependencies.monotonic_clock, started_monotonic),
            cooldown_until=cooldown_until,
            cooldown_kind=cooldown_kind,
            terminal_committed_at=dependencies.clock.now(),
        ),
    )
    if (
        failure.outcome.transition is RoutingTransition.RATE_COOLDOWN
        and failure.retry_after_seconds is None
        and cooldown_until is not None
    ):
        retry_after = max(
            1,
            ceil((cooldown_until - dependencies.clock.now()).total_seconds()),
        )
        return FailureTerminal(
            failure.outcome,
            retry_after,
            retirement_unresolved=failure.retirement_unresolved,
        )
    return failure
