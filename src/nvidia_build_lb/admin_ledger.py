"""Singleton ledger admission and capacity truth from actual database rows."""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.active_routed_requests import ActiveRoutedRequestRegistry
from nvidia_build_lb.admin.schemas import CapacityBlocker
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import (
    AdminEventRow,
    AdminLedgerStateRow,
    UpstreamAttemptReceiptRow,
)


class LedgerCapacityExhaustedError(RuntimeError):
    """Reject a new event-producing operation before domain state changes."""


class LedgerStateUnavailableError(RuntimeError):
    """Reject use of a schema missing its required singleton ledger row."""


@dataclass(frozen=True, slots=True)
class AdminLedgerPolicy:
    """Validated non-secret limits and maintenance timing."""

    event_retention_days: int = 30
    event_max_rows: int = 100_000
    attempt_max_rows: int = 40_000
    prune_batch_size: int = 1_000
    maintenance_interval_seconds: int = 300
    reconciliation_grace_seconds: int = 300

    @property
    def retention_delta(self) -> timedelta:
        """Return the configured event-age boundary."""
        return timedelta(days=self.event_retention_days)

    @property
    def reconciliation_grace(self) -> timedelta:
        """Return the minimum exact-attempt reconciliation floor."""
        return timedelta(seconds=self.reconciliation_grace_seconds)


@dataclass(frozen=True, slots=True)
class LedgerCounts:
    """Actual row counts used by admission and dashboard projection."""

    event_rows: int
    receipt_rows: int
    pending_receipts: int

    def attempt_admission_available(self, policy: AdminLedgerPolicy) -> bool:
        """Require one start, one receipt, and one reserved terminal slot."""
        allocated_event_slots = self.event_rows + self.pending_receipts
        return (
            allocated_event_slots + 2 <= policy.event_max_rows
            and self.receipt_rows + 1 <= policy.attempt_max_rows
        )


@dataclass(frozen=True, slots=True)
class AdminLedger:
    """Shared admission authority and maintenance dependencies."""

    sessions: async_sessionmaker[AsyncSession]
    clock: Clock
    policy: AdminLedgerPolicy = field(default_factory=AdminLedgerPolicy)
    active_requests: ActiveRoutedRequestRegistry = field(
        default_factory=ActiveRoutedRequestRegistry
    )

    async def lock_attempt_admission(self, session: AsyncSession) -> AdminLedgerStateRow:
        """Lock last and admit exactly one new receipt-backed attempt."""
        ledger = await self.lock_state(session)
        counts = await self.counts(session)
        if not self.admission_available(ledger, counts, self.policy):
            raise LedgerCapacityExhaustedError
        return ledger

    async def lock_mutation_admission(self, session: AsyncSession) -> AdminLedgerStateRow:
        """Backpressure every event mutation when routed admission is exhausted."""
        return await self.lock_attempt_admission(session)

    async def lock_terminal(self, session: AsyncSession) -> AdminLedgerStateRow:
        """Serialize a reserved terminal commit without rechecking admission."""
        return await self.lock_state(session)

    @staticmethod
    def admission_available(
        state: AdminLedgerStateRow,
        counts: LedgerCounts,
        policy: AdminLedgerPolicy,
    ) -> bool:
        """Keep durable evidence blockers closed even if caps later increase."""
        permanent = {
            CapacityBlocker.ORPHANED_PENDING.value,
            CapacityBlocker.LEGACY_UNLINKED.value,
        }
        return state.last_capacity_blocker not in permanent and counts.attempt_admission_available(
            policy
        )

    @staticmethod
    async def lock_state(session: AsyncSession) -> AdminLedgerStateRow:
        """Lock and return the required singleton as the final writer lock."""
        ledger = await session.get(AdminLedgerStateRow, 1, with_for_update=True)
        if ledger is None:
            raise LedgerStateUnavailableError
        return ledger

    @staticmethod
    async def counts(session: AsyncSession) -> LedgerCounts:
        """Count actual rows inside the caller's transaction snapshot."""
        event_rows = await session.scalar(select(func.count()).select_from(AdminEventRow))
        receipt_rows = await session.scalar(
            select(func.count()).select_from(UpstreamAttemptReceiptRow)
        )
        pending_receipts = await session.scalar(
            select(func.count())
            .select_from(UpstreamAttemptReceiptRow)
            .where(UpstreamAttemptReceiptRow.terminal_committed_at.is_(None))
        )
        return LedgerCounts(
            event_rows=0 if event_rows is None else event_rows,
            receipt_rows=0 if receipt_rows is None else receipt_rows,
            pending_receipts=0 if pending_receipts is None else pending_receipts,
        )

    async def record_capacity_blocker(
        self,
        session: AsyncSession,
        blocker: CapacityBlocker,
    ) -> AdminLedgerStateRow:
        """Persist one closed maintenance assessment under the singleton lock."""
        ledger = await self.lock_state(session)
        ledger.last_capacity_blocker = blocker.value
        return ledger


def default_admin_ledger(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> AdminLedger:
    """Build the fixed default authority for isolated repository tests."""
    return AdminLedger(sessions=sessions, clock=clock)
