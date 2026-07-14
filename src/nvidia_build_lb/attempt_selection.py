"""Locked eligible-key selection and safe no-eligible classification."""

from datetime import datetime
from math import ceil

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nvidia_build_lb.attempt_types import AttemptStartCommand, CooldownKind
from nvidia_build_lb.credential_types import ResourceNotFoundError
from nvidia_build_lb.db_models import SchedulerStateRow, UpstreamKeyRow
from nvidia_build_lb.scheduler_ring import next_ring_key
from nvidia_build_lb.scheduler_types import NoEligibleReason, NoEligibleUpstreamKeyError


async def select_attempt_key(
    session: AsyncSession,
    scheduler: SchedulerStateRow,
    command: AttemptStartCommand,
    now: datetime,
) -> UpstreamKeyRow:
    """Read the stable ring, then lock only its selected key row."""
    if command.explicit_probe_key_id is not None:
        key = await session.get(
            UpstreamKeyRow,
            command.explicit_probe_key_id,
            with_for_update=True,
        )
        if key is None:
            raise ResourceNotFoundError(
                resource="upstream_key",
                resource_id=command.explicit_probe_key_id,
            )
        return key
    rows = tuple(
        (
            await session.scalars(
                select(UpstreamKeyRow).order_by(UpstreamKeyRow.created_at, UpstreamKeyRow.id)
            )
        ).all()
    )
    selectable = tuple(
        row
        for row in rows
        if row.enabled and not row.quarantined and row.id not in command.excluded_key_ids
    )
    eligible = tuple(
        row for row in selectable if row.cooldown_until is None or row.cooldown_until <= now
    )
    if eligible:
        selected = next_ring_key(rows, eligible, scheduler.cursor_key_id)
        key = await session.get(UpstreamKeyRow, selected.id, with_for_update=True)
        if key is None:
            raise ResourceNotFoundError(resource="upstream_key", resource_id=selected.id)
        return key
    raise _classify_no_eligible(selectable, now)


def _classify_no_eligible(
    rows: tuple[UpstreamKeyRow, ...],
    now: datetime,
) -> NoEligibleUpstreamKeyError:
    cooling = tuple(row for row in rows if row.cooldown_until is not None)
    rate_deadlines = tuple(
        row.cooldown_until
        for row in cooling
        if row.cooldown_kind == CooldownKind.RATE_LIMIT.value and row.cooldown_until is not None
    )
    if rate_deadlines:
        earliest = min(rate_deadlines)
        retry_after = max(1, ceil((earliest - now).total_seconds()))
        return NoEligibleUpstreamKeyError(NoEligibleReason.RATE_COOLDOWN, retry_after)
    if cooling:
        return NoEligibleUpstreamKeyError(NoEligibleReason.TRANSIENT_COOLDOWN)
    return NoEligibleUpstreamKeyError()
