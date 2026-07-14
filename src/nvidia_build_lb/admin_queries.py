"""Secret-free administration aggregate and event projections."""

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    AdminEventListResponse,
    AdminEventRead,
    AdminOverviewRead,
    DownstreamTokenOverview,
    EventOutcome,
    EventType,
    LastStatusClass,
    OverviewStatus,
    UpstreamKeyOverview,
)
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import AdminEventRow, DownstreamTokenRow, UpstreamKeyRow


async def _count(session: AsyncSession, statement: Select[tuple[int]]) -> int:
    value = await session.scalar(statement)
    return 0 if value is None else value


async def read_overview(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> AdminOverviewRead:
    """Read one repeatable secret-free overview snapshot."""
    now = clock.now()
    async with sessions.begin() as session:
        _ = await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        upstream_total = await _count(session, select(func.count()).select_from(UpstreamKeyRow))
        enabled = await _count(
            session,
            select(func.count())
            .select_from(UpstreamKeyRow)
            .where(UpstreamKeyRow.enabled.is_(True)),
        )
        eligible = await _count(
            session,
            select(func.count())
            .select_from(UpstreamKeyRow)
            .where(
                UpstreamKeyRow.enabled.is_(True),
                UpstreamKeyRow.quarantined.is_(False),
                or_(UpstreamKeyRow.cooldown_until.is_(None), UpstreamKeyRow.cooldown_until <= now),
            ),
        )
        cooling = await _count(
            session,
            select(func.count())
            .select_from(UpstreamKeyRow)
            .where(
                UpstreamKeyRow.enabled.is_(True),
                UpstreamKeyRow.cooldown_until > now,
            ),
        )
        degraded = await _count(
            session,
            select(func.count())
            .select_from(UpstreamKeyRow)
            .where(UpstreamKeyRow.health_state == "degraded"),
        )
        downstream_total = await _count(
            session,
            select(func.count()).select_from(DownstreamTokenRow),
        )
        revoked = await _count(
            session,
            select(func.count())
            .select_from(DownstreamTokenRow)
            .where(DownstreamTokenRow.revoked_at.is_not(None)),
        )
        request_count = await _count(
            session,
            select(func.count())
            .select_from(AdminEventRow)
            .where(
                AdminEventRow.event_type == EventType.UPSTREAM_ATTEMPT.value,
                AdminEventRow.outcome_class == EventOutcome.STARTED.value,
            ),
        )
        last_event_at = await session.scalar(select(func.max(AdminEventRow.occurred_at)))
    ready = eligible > 0
    return AdminOverviewRead(
        status=OverviewStatus.OK if ready else OverviewStatus.DEGRADED,
        ready=ready,
        upstream_keys=UpstreamKeyOverview(
            total=upstream_total,
            enabled=enabled,
            eligible=eligible,
            cooling=cooling,
            degraded=degraded,
        ),
        downstream_tokens=DownstreamTokenOverview(
            total=downstream_total,
            active=downstream_total - revoked,
            revoked=revoked,
        ),
        request_count=request_count,
        last_event_at=last_event_at,
        generated_at=now,
    )


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
