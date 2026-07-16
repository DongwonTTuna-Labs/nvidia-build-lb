"""Bounded exact-group event and attempt retention maintenance."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

import anyio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nvidia_build_lb.admin.schemas import CapacityBlocker
from nvidia_build_lb.admin_ledger import AdminLedger
from nvidia_build_lb.admin_maintenance_blockers import (
    classify_blocker,
    classify_permanent_blocker,
)
from nvidia_build_lb.admin_maintenance_candidates import MaintenanceCandidateSelector
from nvidia_build_lb.admin_maintenance_coherence import (
    ATTEMPT_EVENT_TYPES,
    newest_event_ids,
)
from nvidia_build_lb.admin_maintenance_types import (
    FinishDelta,
    MaintenanceFailureReporter,
    MaintenancePass,
    MaintenanceResult,
)
from nvidia_build_lb.db_models import AdminEventRow, AdminLedgerStateRow, UpstreamAttemptReceiptRow
from nvidia_build_lb.service_epoch_monitor import AnyioSleeper, Sleeper

_PASS_WALL_CLOCK_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class AdminMaintenance:
    """Run one exact, row-budgeted and wall-clock-bounded maintenance batch."""

    ledger: AdminLedger

    async def run_once(self) -> MaintenanceResult:
        """Delete several whole groups up to budget without splitting an attempt."""
        now = self.ledger.clock.now()
        with anyio.fail_after(_PASS_WALL_CLOCK_SECONDS):
            return await self._run_transaction(now)

    async def _run_transaction(self, now: datetime) -> MaintenanceResult:
        async with self.ledger.sessions.begin() as session:
            _ = await session.execute(text("SET LOCAL lock_timeout = '2s'"))
            _ = await session.execute(text("SET LOCAL statement_timeout = '5s'"))
            counts_before = await self.ledger.counts(session)
            selector = MaintenanceCandidateSelector(self.ledger)
            protected = await newest_event_ids(session)
            batch = await selector.batch(session, protected, counts_before, now)
            receipt_ids = tuple(
                identity for candidate in batch.attempts for identity in candidate.receipt_ids
            )
            event_ids = tuple(
                identity for candidate in batch.attempts for identity in candidate.event_ids
            ) + tuple(candidate.event_id for candidate in batch.events)
            receipts = tuple(
                ()
                if not receipt_ids
                else (
                    await session.scalars(
                        select(UpstreamAttemptReceiptRow)
                        .where(UpstreamAttemptReceiptRow.started_event_id.in_(receipt_ids))
                        .order_by(UpstreamAttemptReceiptRow.started_event_id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            events = tuple(
                ()
                if not event_ids
                else (
                    await session.scalars(
                        select(AdminEventRow)
                        .where(AdminEventRow.id.in_(event_ids))
                        .order_by(AdminEventRow.id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            ledger_row = await self.ledger.lock_state(session)
            locked_receipts = frozenset(row.started_event_id for row in receipts)
            locked_events = frozenset(row.id for row in events)
            protected = await newest_event_ids(session)
            safe_attempts = tuple(
                candidate
                for candidate in await selector.safe_attempts(
                    session,
                    batch.attempts,
                    protected,
                    now,
                )
                if frozenset(candidate.receipt_ids) <= locked_receipts
                and frozenset(candidate.event_ids) <= locked_events
            )
            events_by_id = {row.id: row for row in events}
            receipts_by_id = {row.started_event_id: row for row in receipts}
            safe_events = tuple(
                candidate
                for candidate in batch.events
                if candidate.event_id in locked_events
                and candidate.event_id not in protected
                and events_by_id[candidate.event_id].event_type not in ATTEMPT_EVENT_TYPES
            )
            for candidate in safe_attempts:
                for event_id in candidate.terminal_event_ids:
                    await session.delete(events_by_id[event_id])
            await session.flush()
            for candidate in safe_attempts:
                for receipt_id in candidate.receipt_ids:
                    await session.delete(receipts_by_id[receipt_id])
            await session.flush()
            for candidate in safe_attempts:
                for event_id in candidate.start_event_ids:
                    await session.delete(events_by_id[event_id])
            for candidate in safe_events:
                await session.delete(events_by_id[candidate.event_id])
            routed_groups = sum(candidate.routed for candidate in safe_attempts)
            ledger_row.rolled_up_routed_request_count += routed_groups
            return await self._finish(
                session,
                ledger_row,
                now,
                FinishDelta(
                    deleted_events=sum(len(candidate.event_ids) for candidate in safe_attempts)
                    + len(safe_events),
                    deleted_receipts=sum(len(candidate.receipt_ids) for candidate in safe_attempts),
                ),
            )

    async def _finish(
        self,
        session: AsyncSession,
        ledger_row: AdminLedgerStateRow,
        now: datetime,
        delta: FinishDelta,
    ) -> MaintenanceResult:
        await session.flush()
        counts = await self.ledger.counts(session)
        raw_capacity_available = counts.attempt_admission_available(self.ledger.policy)
        permanent_blocker = await classify_permanent_blocker(session)
        capacity_available = raw_capacity_available and permanent_blocker is None
        blocker = (
            permanent_blocker
            if permanent_blocker is not None
            else CapacityBlocker.NONE
            if raw_capacity_available
            else await classify_blocker(self.ledger, session, now)
        )
        ledger_row.last_maintenance_completed_at = now
        ledger_row.last_pruned_event_rows = delta.deleted_events
        ledger_row.last_pruned_attempt_rows = delta.deleted_receipts
        ledger_row.last_capacity_blocker = blocker.value
        return MaintenanceResult(
            deleted_event_rows=delta.deleted_events,
            deleted_attempt_rows=delta.deleted_receipts,
            capacity_blocker=blocker,
            capacity_available=capacity_available,
        )


@dataclass(frozen=True, slots=True)
class AdminMaintenancePrePublish:
    """Adapt the startup pass to the service-epoch pre-publish hook."""

    maintenance: MaintenancePass

    async def __call__(self, epoch: UUID) -> None:
        """Run the startup pass after prior-pin cleanup and before publish."""
        del epoch
        _ = await self.maintenance.run_once()


@dataclass(frozen=True, slots=True)
class AdminMaintenanceWorker:
    """Run one bounded pass per interval and retain failure visibility."""

    maintenance: MaintenancePass
    interval_seconds: int
    report_failure: MaintenanceFailureReporter
    sleeper: Sleeper = field(default_factory=AnyioSleeper)

    async def run(self) -> None:
        """Sleep between passes; a failed pass never becomes a busy loop."""
        while True:
            await self.sleeper.sleep(self.interval_seconds)
            try:
                _ = await self.maintenance.run_once()
            except Exception:  # noqa: BLE001 - reporter accepts no unsafe exception data.
                self.report_failure()
