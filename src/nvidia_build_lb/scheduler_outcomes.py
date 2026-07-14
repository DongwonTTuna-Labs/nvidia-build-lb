"""Counter deltas and event outcomes for scheduler terminals."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from nvidia_build_lb.admin.schemas import EventOutcome
from nvidia_build_lb.scheduler_types import TerminalOutcome


@dataclass(frozen=True, slots=True)
class OutcomeChange:
    """Exact counter and event projection for one terminal outcome."""

    success_delta: int
    failure_delta: int
    event_outcome: EventOutcome


_CHANGES: Final[Mapping[TerminalOutcome, OutcomeChange]] = MappingProxyType(
    {
        TerminalOutcome.SUCCEEDED: OutcomeChange(1, 0, EventOutcome.SUCCEEDED),
        TerminalOutcome.FAILED: OutcomeChange(0, 1, EventOutcome.FAILED),
        TerminalOutcome.CANCELLED: OutcomeChange(0, 1, EventOutcome.CANCELLED),
    }
)


def outcome_change(outcome: TerminalOutcome) -> OutcomeChange:
    """Return the closed delta for one terminal outcome."""
    return _CHANGES[outcome]
