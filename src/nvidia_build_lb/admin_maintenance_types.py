"""Closed values and protocols shared by ledger maintenance components."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from nvidia_build_lb.admin.schemas import CapacityBlocker


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    """Safe outcome of one successful bounded maintenance transaction."""

    deleted_event_rows: int
    deleted_attempt_rows: int
    capacity_blocker: CapacityBlocker
    capacity_available: bool


class MaintenanceFailureReporter(Protocol):
    """Emit one closed recurring-maintenance failure signal."""

    def __call__(self) -> None:
        """Report without receiving an exception or database value."""
        ...


class MaintenancePass(Protocol):
    """Run one bounded maintenance transaction."""

    async def run_once(self) -> MaintenanceResult:
        """Return one safe pass result or raise a closed operational failure."""
        ...


@dataclass(frozen=True, slots=True)
class AttemptCandidate:
    """One exact receipt and event group eligible for bounded pruning."""

    request_id: str
    receipt_ids: tuple[UUID, ...]
    start_event_ids: tuple[UUID, ...]
    terminal_event_ids: tuple[UUID, ...]
    latest_terminal_at: datetime
    routed: bool

    @property
    def event_ids(self) -> tuple[UUID, ...]:
        """Return every event identity in deletion order-independent form."""
        return (*self.start_event_ids, *self.terminal_event_ids)

    @property
    def row_cost(self) -> int:
        """Return the total event and receipt rows consumed by the group."""
        return len(self.event_ids) + len(self.receipt_ids)


@dataclass(frozen=True, slots=True)
class EventCandidate:
    """One non-attempt event eligible for bounded pruning."""

    event_id: UUID


@dataclass(frozen=True, slots=True)
class MaintenanceBatch:
    """Whole attempt groups plus standalone events within one row budget."""

    attempts: tuple[AttemptCandidate, ...] = ()
    events: tuple[EventCandidate, ...] = ()

    @property
    def row_cost(self) -> int:
        """Return the exact event-plus-receipt rows selected for deletion."""
        return sum(candidate.row_cost for candidate in self.attempts) + len(self.events)


@dataclass(frozen=True, slots=True)
class MaintenanceGroup:
    """One aggregate receipt group selected by the bounded SQL query."""

    kind: str
    identity: str
    latest_terminal_at: datetime
    receipt_count: int


@dataclass(frozen=True, slots=True)
class FinishDelta:
    """Rows deleted by one maintenance pass before final state projection."""

    deleted_events: int = 0
    deleted_receipts: int = 0
