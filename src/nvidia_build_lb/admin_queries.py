"""Secret-free administration aggregate and event projections."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    AdminEventListResponse,
    AdminEventRead,
    AdminOverviewRead,
    EventOutcome,
    EventType,
    LastStatusClass,
)
from nvidia_build_lb.admin_dashboard import read_dashboard
from nvidia_build_lb.admin_ledger import AdminLedgerPolicy
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import AdminEventRow


async def read_overview(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    policy: AdminLedgerPolicy | None = None,
) -> AdminOverviewRead:
    """Read the legacy shape from the same capacity and aggregate predicate."""
    return (await read_dashboard(sessions, clock, policy or AdminLedgerPolicy())).overview


def _event_read(row: AdminEventRow) -> AdminEventRead:
    status = None if row.status_class is None else LastStatusClass(row.status_class)
    return AdminEventRead(
        id=row.id,
        request_id=row.request_id,
        event_type=EventType(row.event_type),
        upstream_key_id=row.upstream_key_id,
        downstream_token_id=row.downstream_token_id,
        outcome_class=EventOutcome(row.outcome_class),
        status_class=status,
        latency_ms=row.latency_ms,
        occurred_at=row.occurred_at,
    )


async def read_events(
    sessions: async_sessionmaker[AsyncSession],
) -> AdminEventListResponse:
    """Read the newest one hundred safe events in canonical order."""
    async with sessions() as session:
        rows = tuple(
            (
                await session.scalars(
                    select(AdminEventRow)
                    .order_by(AdminEventRow.occurred_at.desc(), AdminEventRow.id.desc())
                    .limit(100)
                )
            ).all()
        )
    return AdminEventListResponse(items=tuple(_event_read(row) for row in rows))
