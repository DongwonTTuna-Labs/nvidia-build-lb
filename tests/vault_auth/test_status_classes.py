from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    EventOutcome,
    LastStatusClass,
    UpstreamKeyCreateRequest,
)
from nvidia_build_lb.admin_queries import read_events
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import AdminEventRow
from nvidia_build_lb.scheduler_state import (
    AttemptTerminal,
    SchedulerDependencies,
    SchedulerStateRepository,
    TerminalOutcome,
)
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


@pytest.mark.parametrize(
    "status_class",
    [LastStatusClass.UPSTREAM_BAD_GATEWAY, LastStatusClass.DELIVERY_FAILED],
)
async def test_status_round_trips_through_legacy_key_and_event_projections(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
    status_class: LastStatusClass,
) -> None:
    # Given: the full closed persisted status set and one committed upstream attempt.
    expected = {
        "success",
        "invalid_credential",
        "credits_exhausted",
        "rate_limited",
        "request_rejected",
        "timeout",
        "upstream_unavailable",
        "upstream_bad_gateway",
        "upstream_internal_error",
        "upstream_protocol_error",
        "cancelled",
        "delivery_failed",
    }
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    created = await upstream.create(
        UpstreamKeyCreateRequest(key="status-round-trip-key"),
        request_id="status-key-create",
    )
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    request_id = f"status-attempt-{status_class.value}"
    lease = await scheduler.begin_attempt(created.id, request_id)

    # When: each previously drift-prone terminal class is persisted.
    await scheduler.finish_attempt(
        lease,
        AttemptTerminal(
            TerminalOutcome.FAILED,
            status_class,
            12,
            (
                fixed_clock.now() + timedelta(seconds=5)
                if status_class is LastStatusClass.UPSTREAM_BAD_GATEWAY
                else None
            ),
        ),
    )

    # Then: enum closure, raw rows, and both safe projections preserve the exact class.
    listed = await upstream.list_all()
    events = await read_events(migrated_session_factory)
    async with migrated_session_factory() as session:
        terminal = await session.scalar(
            select(AdminEventRow).where(
                AdminEventRow.request_id == request_id,
                AdminEventRow.outcome_class == EventOutcome.FAILED.value,
            )
        )
    assert {status.value for status in LastStatusClass} == expected
    assert listed.items[0].last_status_class is status_class
    assert terminal is not None
    assert terminal.status_class == status_class.value
    projected_terminal = tuple(
        item
        for item in events.items
        if item.request_id == request_id and item.outcome_class is EventOutcome.FAILED
    )
    assert projected_terminal[0].status_class is status_class
