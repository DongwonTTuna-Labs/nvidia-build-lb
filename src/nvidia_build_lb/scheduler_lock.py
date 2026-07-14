"""Canonical first lock for every routing-state writer."""

from sqlalchemy.ext.asyncio import AsyncSession

from nvidia_build_lb.db_models import SchedulerStateRow
from nvidia_build_lb.scheduler_types import SchedulerStateUnavailableError


async def lock_scheduler_state(session: AsyncSession) -> SchedulerStateRow:
    """Lock and return the required scheduler singleton."""
    scheduler = await session.get(SchedulerStateRow, 1, with_for_update=True)
    if scheduler is None:
        raise SchedulerStateUnavailableError
    return scheduler
