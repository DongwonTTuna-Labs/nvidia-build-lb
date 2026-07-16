"""Bounded database projection for host-owned operational readiness checks."""

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    AdminOperatorReadinessRead,
    ReadinessCause,
    RuntimeState,
)
from nvidia_build_lb.admin_dashboard_projection import ledger_condition
from nvidia_build_lb.admin_ledger import (
    AdminLedger,
    AdminLedgerPolicy,
    LedgerStateUnavailableError,
)
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import AdminLedgerStateRow, UpstreamKeyRow


async def read_operator_readiness(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    policy: AdminLedgerPolicy,
) -> AdminOperatorReadinessRead:
    """Read only the singleton, capacity counts, and eligible-key count."""
    as_of = clock.now()
    async with sessions.begin() as session:
        _ = await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        ledger_row = await session.get(AdminLedgerStateRow, 1)
        if ledger_row is None:
            raise LedgerStateUnavailableError
        counts = await AdminLedger.counts(session)
        eligible_count = await session.scalar(
            select(func.count())
            .select_from(UpstreamKeyRow)
            .where(
                UpstreamKeyRow.enabled.is_(True),
                UpstreamKeyRow.quarantined.is_(False),
                or_(
                    UpstreamKeyRow.cooldown_until.is_(None),
                    UpstreamKeyRow.cooldown_until <= as_of,
                ),
            )
        )

    capacity_available = AdminLedger.admission_available(ledger_row, counts, policy)
    condition = ledger_condition(
        ledger_row,
        policy,
        as_of,
        capacity_available=capacity_available,
    )
    readiness_cause = (
        ReadinessCause.LEDGER_CAPACITY_EXHAUSTED
        if not capacity_available
        else ReadinessCause.NO_ELIGIBLE_UPSTREAM
        if not eligible_count
        else ReadinessCause.READY
    )
    return AdminOperatorReadinessRead(
        runtime_state=RuntimeState.OPERATIONAL,
        readiness_cause=readiness_cause,
        ledger_status=condition.status,
        capacity_blocker=condition.capacity_blocker,
    )
