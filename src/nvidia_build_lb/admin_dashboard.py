"""Canonical coherent administration dashboard database snapshot."""

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    AdminDashboardEventListResponse,
    AdminDashboardEventRead,
    AdminDashboardRead,
    DownstreamTokenListResponse,
    EventOutcome,
    EventType,
    LastStatusClass,
    ReadinessCause,
    RuntimeState,
    UpstreamKeyListResponse,
)
from nvidia_build_lb.admin_dashboard_projection import (
    LedgerProjectionInput,
    OverviewProjectionInput,
    ledger_read,
    overview_read,
)
from nvidia_build_lb.admin_ledger import (
    AdminLedger,
    AdminLedgerPolicy,
    LedgerStateUnavailableError,
)
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import (
    AdminEventRow,
    AdminLedgerStateRow,
    DownstreamTokenRow,
    UpstreamKeyRow,
)
from nvidia_build_lb.downstream_tokens import downstream_token_read
from nvidia_build_lb.upstream_key_views import upstream_key_read


async def read_dashboard(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    policy: AdminLedgerPolicy,
) -> AdminDashboardRead:
    """Materialize every browser fact inside one read-only repeatable snapshot."""
    as_of = clock.now()
    async with sessions.begin() as session:
        _ = await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        ledger_row = await session.get(AdminLedgerStateRow, 1)
        if ledger_row is None:
            raise LedgerStateUnavailableError
        key_rows = tuple(
            (
                await session.scalars(
                    select(UpstreamKeyRow).order_by(
                        UpstreamKeyRow.created_at.asc(),
                        UpstreamKeyRow.id.asc(),
                    )
                )
            ).all()
        )
        token_rows = tuple(
            (
                await session.scalars(
                    select(DownstreamTokenRow).order_by(
                        DownstreamTokenRow.created_at.asc(),
                        DownstreamTokenRow.id.asc(),
                    )
                )
            ).all()
        )
        event_rows = tuple(
            (
                await session.scalars(
                    select(AdminEventRow)
                    .order_by(AdminEventRow.occurred_at.desc(), AdminEventRow.id.desc())
                    .limit(100)
                )
            ).all()
        )
        counts = await AdminLedger.counts(session)
        oldest_event_at = await session.scalar(select(func.min(AdminEventRow.occurred_at)))
        retained_request_count = await _retained_routed_request_count(session)

    upstream = UpstreamKeyListResponse(
        items=tuple(upstream_key_read(row, as_of) for row in key_rows)
    )
    downstream = DownstreamTokenListResponse(
        items=tuple(downstream_token_read(row) for row in token_rows)
    )
    events = _dashboard_events(event_rows, key_rows)
    capacity_available = AdminLedger.admission_available(ledger_row, counts, policy)
    ledger = ledger_read(
        LedgerProjectionInput(
            state=ledger_row,
            counts=counts,
            policy=policy,
            as_of=as_of,
            oldest_event_at=oldest_event_at,
            capacity_available=capacity_available,
        )
    )
    overview = overview_read(
        OverviewProjectionInput(
            upstream=upstream,
            downstream=downstream,
            events=events,
            rolled_up=ledger_row.rolled_up_routed_request_count,
            retained=retained_request_count,
            generated_at=as_of,
            capacity_available=capacity_available,
        )
    )
    readiness_cause = (
        ReadinessCause.LEDGER_CAPACITY_EXHAUSTED
        if not capacity_available
        else ReadinessCause.NO_ELIGIBLE_UPSTREAM
        if overview.upstream_keys.eligible == 0
        else ReadinessCause.READY
    )
    return AdminDashboardRead(
        runtime_state=RuntimeState.OPERATIONAL,
        readiness_cause=readiness_cause,
        ledger=ledger,
        overview=overview,
        upstream_keys=upstream,
        downstream_tokens=downstream,
        events=events,
    )


async def _retained_routed_request_count(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count(func.distinct(AdminEventRow.request_id)))
        .select_from(AdminEventRow)
        .where(
            AdminEventRow.event_type == EventType.UPSTREAM_ATTEMPT.value,
            AdminEventRow.outcome_class == EventOutcome.STARTED.value,
        )
    )
    return 0 if count is None else count


def _dashboard_events(
    rows: tuple[AdminEventRow, ...],
    keys: tuple[UpstreamKeyRow, ...],
) -> AdminDashboardEventListResponse:
    live_fingerprints = {row.id: row.fingerprint for row in keys}
    items: list[AdminDashboardEventRead] = []
    for row in rows:
        raw_fingerprint = row.upstream_key_fingerprint
        if raw_fingerprint is None and row.upstream_key_id is not None:
            raw_fingerprint = live_fingerprints.get(row.upstream_key_id)
        items.append(
            AdminDashboardEventRead(
                id=row.id,
                request_id=row.request_id,
                event_type=EventType(row.event_type),
                upstream_key_id=row.upstream_key_id,
                downstream_token_id=row.downstream_token_id,
                outcome_class=EventOutcome(row.outcome_class),
                status_class=(
                    None if row.status_class is None else LastStatusClass(row.status_class)
                ),
                latency_ms=row.latency_ms,
                occurred_at=row.occurred_at,
                upstream_key_fingerprint=(
                    None if raw_fingerprint is None else f"sha256:{raw_fingerprint}"
                ),
                attempt_started_event_id=row.attempt_started_event_id,
            )
        )
    return AdminDashboardEventListResponse(items=tuple(items))
