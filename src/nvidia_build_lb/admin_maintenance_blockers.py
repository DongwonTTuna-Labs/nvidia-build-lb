"""Bounded maintenance-pressure classification queries."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nvidia_build_lb.admin.schemas import CapacityBlocker
from nvidia_build_lb.admin_ledger import AdminLedger
from nvidia_build_lb.admin_maintenance_coherence import has_legacy_unlinked_attempt
from nvidia_build_lb.db_models import UpstreamAttemptReceiptRow, UpstreamLivePinRow


async def classify_permanent_blocker(session: AsyncSession) -> CapacityBlocker | None:
    """Return a durable blocker that cap changes must never hide."""
    orphaned = await session.scalar(
        select(UpstreamAttemptReceiptRow.started_event_id)
        .outerjoin(
            UpstreamLivePinRow,
            UpstreamLivePinRow.started_event_id == UpstreamAttemptReceiptRow.started_event_id,
        )
        .where(
            UpstreamAttemptReceiptRow.terminal_committed_at.is_(None),
            UpstreamLivePinRow.started_event_id.is_(None),
        )
        .limit(1)
    )
    if orphaned is not None:
        return CapacityBlocker.ORPHANED_PENDING
    if await has_legacy_unlinked_attempt(session):
        return CapacityBlocker.LEGACY_UNLINKED
    return None


async def classify_blocker(
    ledger: AdminLedger,
    session: AsyncSession,
    now: datetime,
) -> CapacityBlocker:
    """Classify pressure with bounded existence queries, never a full row load."""
    permanent = await classify_permanent_blocker(session)
    if permanent is not None:
        return permanent
    pending = await session.scalar(
        select(UpstreamAttemptReceiptRow.started_event_id)
        .where(UpstreamAttemptReceiptRow.terminal_committed_at.is_(None))
        .limit(1)
    )
    active_ids = ledger.active_requests.snapshot()
    active = pending is not None
    if active_ids and not active:
        active = (
            await session.scalar(
                select(UpstreamAttemptReceiptRow.started_event_id)
                .where(
                    UpstreamAttemptReceiptRow.explicit_probe_key_id.is_(None),
                    UpstreamAttemptReceiptRow.request_id.in_(active_ids),
                )
                .limit(1)
            )
            is not None
        )
    if active:
        return CapacityBlocker.ACTIVE_ATTEMPTS
    in_grace = await session.scalar(
        select(UpstreamAttemptReceiptRow.started_event_id)
        .where(
            UpstreamAttemptReceiptRow.terminal_committed_at
            > now - ledger.policy.reconciliation_grace
        )
        .limit(1)
    )
    return (
        CapacityBlocker.RECONCILIATION_GRACE
        if in_grace is not None
        else CapacityBlocker.LOCK_CONTENTION
    )
