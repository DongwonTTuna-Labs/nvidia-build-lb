"""Safe completed-attempt observations attached to routed results."""

from dataclasses import dataclass, field, replace

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.attempt_types import AttemptLease
from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.routing_models import (
    RoutedAttemptObservation,
    RoutedFailure,
    RoutedJson,
    RoutedStream,
)
from nvidia_build_lb.scheduler_state import TerminalOutcome


@dataclass(slots=True)
class RoutedObservations:
    """Accumulate only durably completed real attempts in ordinal order."""

    _values: list[RoutedAttemptObservation] = field(default_factory=list)

    def completed(
        self,
        routed: RoutedJson | RoutedStream,
        lease: AttemptLease,
        attempt_count: int,
    ) -> RoutedJson | RoutedStream:
        """Attach prior failures and, for JSON, the durable final success."""
        if isinstance(routed, RoutedJson):
            success = RoutedAttemptObservation(
                key_id=lease.key_id,
                attempt_ordinal=attempt_count,
                safe_status_class=LastStatusClass.SUCCESS,
                terminal_outcome=TerminalOutcome.SUCCEEDED,
            )
            return replace(routed, attempt_observations=(*self._values, success))
        return replace(routed, attempt_observations=tuple(self._values))

    def record_failure(
        self,
        failure: FailureTerminal,
        lease: AttemptLease,
        attempt_count: int,
    ) -> None:
        """Record one already-persisted failure without provider text."""
        self._values.append(
            RoutedAttemptObservation(
                key_id=lease.key_id,
                attempt_ordinal=attempt_count,
                safe_status_class=(
                    failure.outcome.persisted_status or LastStatusClass.UPSTREAM_PROTOCOL_ERROR
                ),
                terminal_outcome=TerminalOutcome.FAILED,
            )
        )

    def failure(self, terminal: FailureTerminal, attempt_count: int) -> RoutedFailure:
        """Attach every actual attempt to the selected public failure."""
        return RoutedFailure(terminal, attempt_count, tuple(self._values))
