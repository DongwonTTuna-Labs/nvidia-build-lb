from datetime import timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    DownstreamScope,
    DownstreamTokenIssueRequest,
    EventOutcome,
    EventType,
    OverviewStatus,
    UpstreamKeyCreateRequest,
)
from nvidia_build_lb.admin_credentials import CredentialRepositories
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import AdminEventRow
from nvidia_build_lb.downstream_tokens import (
    DownstreamTokenDependencies,
    DownstreamTokenRepository,
    SystemTokenHexSource,
)
from nvidia_build_lb.scheduler_state import SchedulerDependencies, SchedulerStateRepository
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


def _repositories(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    clock: Clock,
) -> CredentialRepositories:
    upstream = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock))
    downstream = DownstreamTokenRepository(
        DownstreamTokenDependencies(sessions, clock, SystemTokenHexSource())
    )
    return CredentialRepositories(upstream, downstream, sessions, clock)


async def test_overview_projects_current_rows_and_durable_attempt_history(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one eligible key, one active token, one revoked token, and one crash gap.
    repositories = _repositories(migrated_session_factory, vault, fixed_clock)
    created = await repositories.upstream.create(
        UpstreamKeyCreateRequest(key="overview-key"),
        request_id="overview-key-create",
    )
    await repositories.upstream.enable(created.id, request_id="overview-key-enable")
    active = await repositories.downstream.issue(
        DownstreamTokenIssueRequest(
            label="overview-active",
            scopes=(DownstreamScope.MODELS_READ,),
        ),
        request_id="overview-active-issue",
    )
    revoked = await repositories.downstream.issue(
        DownstreamTokenIssueRequest(
            label="overview-revoked",
            scopes=(DownstreamScope.CHAT_WRITE,),
        ),
        request_id="overview-revoked-issue",
    )
    await repositories.downstream.revoke(revoked.id, request_id="overview-revoke")
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    _ = await scheduler.begin_attempt(created.id, request_id="overview-crash-gap")

    # When: the exact administration overview is projected.
    overview = await repositories.overview()

    # Then: readiness and counts derive from current rows plus durable started attempts.
    assert overview.status is OverviewStatus.OK
    assert overview.ready is True
    assert overview.upstream_keys.model_dump() == {
        "total": 1,
        "enabled": 1,
        "eligible": 1,
        "cooling": 0,
        "degraded": 0,
    }
    assert overview.downstream_tokens.model_dump() == {
        "total": 2,
        "active": 1,
        "revoked": 1,
    }
    assert active.revoked_at is None
    assert overview.request_count == 1
    assert overview.last_event_at == fixed_clock.now()
    assert overview.generated_at == fixed_clock.now()


async def test_overview_retains_attempt_count_after_key_deletion(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one attempted key that is later disabled and deleted.
    repositories = _repositories(migrated_session_factory, vault, fixed_clock)
    created = await repositories.upstream.create(
        UpstreamKeyCreateRequest(key="deleted-attempt-key"),
        request_id="deleted-key-create",
    )
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    _ = await scheduler.begin_attempt(created.id, request_id="deleted-key-attempt")
    await repositories.upstream.delete(created.id, request_id="deleted-key-delete")

    # When: the overview is read after the key row is gone.
    overview = await repositories.overview()

    # Then: historical logical attempts remain durable while current key total is zero.
    assert overview.upstream_keys.total == 0
    assert overview.request_count == 1
    assert overview.status is OverviewStatus.DEGRADED
    assert overview.ready is False


async def test_events_projects_only_newest_one_hundred_in_descending_order(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one hundred and one safe events with deterministic distinct times and IDs.
    repositories = _repositories(migrated_session_factory, vault, fixed_clock)
    async with migrated_session_factory.begin() as session:
        for index in range(101):
            session.add(
                AdminEventRow(
                    id=UUID(int=index + 1),
                    request_id=f"event-{index}",
                    event_type=EventType.UPSTREAM_ATTEMPT.value,
                    upstream_key_id=None,
                    downstream_token_id=None,
                    outcome_class=EventOutcome.STARTED.value,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=fixed_clock.now() + timedelta(seconds=index),
                )
            )

    # When: the administration event collection is projected.
    events = await repositories.events()

    # Then: the oldest row is excluded and every safe row is newest-first.
    assert len(events.items) == 100
    assert events.items[0].request_id == "event-100"
    assert events.items[-1].request_id == "event-1"
    assert all("ciphertext" not in item.model_dump_json() for item in events.items)
