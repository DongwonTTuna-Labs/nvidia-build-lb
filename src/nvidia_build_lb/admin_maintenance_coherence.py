"""Exact-group and newest-window coherence checks for ledger maintenance."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from nvidia_build_lb.admin.schemas import EventOutcome, EventType
from nvidia_build_lb.admin_maintenance_types import AttemptCandidate
from nvidia_build_lb.db_models import AdminEventRow, UpstreamAttemptReceiptRow

ATTEMPT_EVENT_TYPES = (EventType.UPSTREAM_ATTEMPT.value, EventType.UPSTREAM_PROBE.value)


async def newest_event_ids(session: AsyncSession) -> frozenset[UUID]:
    """Return identities protected by the newest-100 dashboard window."""
    return frozenset(
        (
            await session.scalars(
                select(AdminEventRow.id)
                .order_by(AdminEventRow.occurred_at.desc(), AdminEventRow.id.desc())
                .limit(100)
            )
        ).all()
    )


def candidate_matches_rows(
    candidate: AttemptCandidate,
    receipts: tuple[UpstreamAttemptReceiptRow, ...],
    events: tuple[AdminEventRow, ...],
) -> bool:
    """Confirm one candidate against a batch-fetched complete request group."""
    if frozenset(row.started_event_id for row in receipts) != frozenset(candidate.receipt_ids):
        return False
    by_id = {row.id: row for row in events}
    if frozenset(by_id) != frozenset(candidate.event_ids):
        return False
    for receipt in receipts:
        start = by_id[receipt.started_event_id]
        terminal = by_id[receipt.terminal_event_id]
        if (
            start.request_id != receipt.request_id
            or terminal.request_id != receipt.request_id
            or start.outcome_class != EventOutcome.STARTED.value
            or start.attempt_started_event_id is not None
            or terminal.attempt_started_event_id != receipt.started_event_id
            or terminal.outcome_class == EventOutcome.STARTED.value
        ):
            return False
    return True


async def has_legacy_unlinked_attempt(
    session: AsyncSession,
) -> bool:
    """Detect attempt events absent from either exact receipt foreign-key edge."""
    started = aliased(UpstreamAttemptReceiptRow)
    terminal = aliased(UpstreamAttemptReceiptRow)
    event_id = await session.scalar(
        select(AdminEventRow.id)
        .outerjoin(started, started.started_event_id == AdminEventRow.id)
        .outerjoin(terminal, terminal.terminal_event_id == AdminEventRow.id)
        .where(
            AdminEventRow.event_type.in_(ATTEMPT_EVENT_TYPES),
            started.started_event_id.is_(None),
            terminal.started_event_id.is_(None),
        )
        .limit(1)
    )
    return event_id is not None
