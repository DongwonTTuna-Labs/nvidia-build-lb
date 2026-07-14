from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    UpstreamKeyCreateRequest,
)
from nvidia_build_lb.credential_types import Clock, ResourceConflictError
from nvidia_build_lb.db_models import AdminEventRow, SchedulerStateRow, UpstreamKeyRow
from nvidia_build_lb.scheduler_state import (
    AttemptTerminal,
    NoEligibleUpstreamKeyError,
    SchedulerDependencies,
    SchedulerStateRepository,
    TerminalOutcome,
)
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


async def _persist_key(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    clock: Clock,
    credential: str = "scheduler-synthetic-key",
) -> UpstreamKeyRow:
    repository = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock))
    created = await repository.create(
        UpstreamKeyCreateRequest(key=credential),
        request_id=f"create-{credential}",
    )
    async with sessions() as session:
        row = await session.get(UpstreamKeyRow, created.id)
        assert row is not None
        return row


async def test_pre_network_attempt_and_cursor_survive_repository_restart_as_crash_gap(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one persisted key and a fresh scheduler repository.
    key = await _persist_key(migrated_session_factory, vault, fixed_clock)
    dependencies = SchedulerDependencies(migrated_session_factory, fixed_clock)
    repository = SchedulerStateRepository(dependencies)

    # When: pre-network state commits and a new repository instance reads it without finishing.
    lease = await repository.begin_attempt(key.id, request_id="public-request")
    restarted = SchedulerStateRepository(dependencies)
    cursor = await restarted.current_cursor()

    # Then: cursor, request counter, last-use, and started event form a durable crash gap.
    async with migrated_session_factory() as session:
        persisted = await session.get(UpstreamKeyRow, key.id)
        events = tuple(
            (
                await session.scalars(
                    select(AdminEventRow)
                    .where(AdminEventRow.request_id == "public-request")
                    .order_by(AdminEventRow.occurred_at.asc(), AdminEventRow.id.asc())
                )
            ).all()
        )
    assert persisted is not None
    assert cursor == key.id == lease.key_id
    assert persisted.request_count == 1
    assert persisted.success_count == persisted.failure_count == 0
    assert persisted.last_used_at == fixed_clock.now()
    assert len(events) == 1
    assert events[0].event_type == EventType.UPSTREAM_ATTEMPT.value
    assert events[0].outcome_class == EventOutcome.STARTED.value


async def test_terminal_success_commits_separately_and_preserves_counter_invariant(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one committed pre-network attempt.
    key = await _persist_key(migrated_session_factory, vault, fixed_clock)
    repository = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    lease = await repository.begin_attempt(key.id, request_id="success-request")
    terminal = AttemptTerminal(
        outcome=TerminalOutcome.SUCCEEDED,
        status_class=LastStatusClass.SUCCESS,
        latency_ms=17,
        cooldown_until=None,
    )

    # When: the terminal transaction is committed.
    await repository.finish_attempt(lease, terminal)

    # Then: exactly one success and terminal event are durable after the started event.
    async with migrated_session_factory() as session:
        persisted = await session.get(UpstreamKeyRow, key.id)
        outcomes = tuple(
            (
                await session.scalars(
                    select(AdminEventRow.outcome_class)
                    .where(AdminEventRow.request_id == "success-request")
                    .order_by(AdminEventRow.occurred_at.asc(), AdminEventRow.id.asc())
                )
            ).all()
        )
    assert persisted is not None
    assert persisted.request_count == persisted.success_count == 1
    assert persisted.failure_count == 0
    assert persisted.health_state == HealthState.HEALTHY.value
    assert set(outcomes) == {EventOutcome.STARTED.value, EventOutcome.SUCCEEDED.value}


async def test_terminal_failure_is_once_only_and_never_decrements_attempts(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one committed pre-network attempt and a rate-limited terminal state.
    key = await _persist_key(migrated_session_factory, vault, fixed_clock)
    repository = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    lease = await repository.begin_attempt(key.id, request_id="failed-request")
    terminal = AttemptTerminal(
        outcome=TerminalOutcome.FAILED,
        status_class=LastStatusClass.RATE_LIMITED,
        latency_ms=11,
        cooldown_until=fixed_clock.now(),
    )

    # When: terminal completion is attempted twice.
    await repository.finish_attempt(lease, terminal)
    with pytest.raises(ResourceConflictError):
        await repository.finish_attempt(lease, terminal)

    # Then: the durable attempt remains one request and one failure.
    async with migrated_session_factory() as session:
        persisted = await session.get(UpstreamKeyRow, key.id)
    assert persisted is not None
    assert persisted.request_count == persisted.failure_count == 1
    assert persisted.success_count == 0
    assert persisted.last_status_class == LastStatusClass.RATE_LIMITED.value


async def test_late_public_success_cannot_recover_legacy_quarantine(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key = await _persist_key(
        migrated_session_factory,
        vault,
        fixed_clock,
        "legacy-late-public-success",
    )
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    await upstream.enable(key.id, request_id="legacy-late-enable")
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    invalid = await scheduler.begin_next_attempt("legacy-public-invalid")
    late_success = await scheduler.begin_next_attempt("legacy-public-late-success")
    await scheduler.finish_attempt(
        invalid,
        AttemptTerminal(
            TerminalOutcome.FAILED,
            LastStatusClass.INVALID_CREDENTIAL,
            1,
            None,
        ),
    )
    await scheduler.finish_attempt(
        late_success,
        AttemptTerminal(
            TerminalOutcome.SUCCEEDED,
            LastStatusClass.SUCCESS,
            1,
            None,
        ),
    )

    async with migrated_session_factory() as session:
        quarantined = await session.get(UpstreamKeyRow, key.id)
    assert quarantined is not None
    assert quarantined.quarantined is True
    assert quarantined.health_state == HealthState.DEGRADED.value
    assert quarantined.last_status_class == LastStatusClass.INVALID_CREDENTIAL.value

    probe = await scheduler.begin_attempt(key.id, "legacy-explicit-probe")
    await scheduler.finish_attempt(
        probe,
        AttemptTerminal(
            TerminalOutcome.SUCCEEDED,
            LastStatusClass.SUCCESS,
            1,
            None,
        ),
    )
    async with migrated_session_factory() as session:
        recovered = await session.get(UpstreamKeyRow, key.id)
    assert recovered is not None
    assert recovered.quarantined is False
    assert recovered.health_state == HealthState.HEALTHY.value
    assert recovered.last_status_class == LastStatusClass.SUCCESS.value


async def test_next_attempt_excludes_disabled_quarantined_and_cooling_keys(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one key in each excluded state and one eligible key.
    disabled = await _persist_key(
        migrated_session_factory,
        vault,
        fixed_clock,
        "scheduler-disabled-key",
    )
    quarantined = await _persist_key(
        migrated_session_factory,
        vault,
        fixed_clock,
        "scheduler-quarantined-key",
    )
    cooling = await _persist_key(
        migrated_session_factory,
        vault,
        fixed_clock,
        "scheduler-cooling-key",
    )
    eligible = await _persist_key(
        migrated_session_factory,
        vault,
        fixed_clock,
        "scheduler-eligible-key",
    )
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    for key in (quarantined, cooling, eligible):
        await upstream.enable(key.id, request_id=f"enable-{key.id}")
    quarantine_lease = await scheduler.begin_attempt(quarantined.id, "quarantine-attempt")
    await scheduler.finish_attempt(
        quarantine_lease,
        AttemptTerminal(
            TerminalOutcome.FAILED,
            LastStatusClass.INVALID_CREDENTIAL,
            1,
            None,
        ),
    )
    cooling_lease = await scheduler.begin_attempt(cooling.id, "cooling-attempt")
    await scheduler.finish_attempt(
        cooling_lease,
        AttemptTerminal(
            TerminalOutcome.FAILED,
            LastStatusClass.RATE_LIMITED,
            1,
            fixed_clock.now() + timedelta(minutes=1),
        ),
    )

    # When: the public scheduler atomically reserves its next attempt.
    selected = await scheduler.begin_next_attempt("eligible-attempt")

    # Then: only the enabled, non-quarantined, non-cooling key is selected.
    assert selected.key_id == eligible.id
    async with migrated_session_factory() as session:
        persisted_disabled = await session.get(UpstreamKeyRow, disabled.id)
    assert persisted_disabled is not None
    assert persisted_disabled.request_count == 0


async def test_next_attempt_preserves_quarantined_cursor_ring_successor(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    keys = tuple(
        [
            await _persist_key(
                migrated_session_factory,
                vault,
                fixed_clock,
                f"legacy-stable-ring-{index}",
            )
            for index in range(3)
        ]
    )
    ordered = tuple(sorted(keys, key=lambda row: row.id.int))
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    for key in ordered:
        await upstream.enable(key.id, request_id=f"legacy-ring-enable-{key.id}")
    async with migrated_session_factory.begin() as session:
        cursor = await session.get(SchedulerStateRow, 1, with_for_update=True)
        assert cursor is not None
        cursor.cursor_key_id = ordered[0].id
    failed = await scheduler.begin_attempt(ordered[1].id, "legacy-ring-failed")
    await scheduler.finish_attempt(
        failed,
        AttemptTerminal(
            TerminalOutcome.FAILED,
            LastStatusClass.INVALID_CREDENTIAL,
            1,
            None,
        ),
    )

    selected = await scheduler.begin_next_attempt("legacy-ring-alternate")

    assert selected.key_id == ordered[2].id


async def test_next_attempt_returns_typed_error_without_mutating_when_none_is_eligible(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: a database whose only upstream key remains disabled.
    disabled = await _persist_key(
        migrated_session_factory,
        vault,
        fixed_clock,
        "scheduler-only-disabled-key",
    )
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )

    # When: public selection is requested with no eligible row.
    with pytest.raises(NoEligibleUpstreamKeyError) as captured:
        _ = await scheduler.begin_next_attempt("no-eligible-attempt")

    # Then: the typed outcome leaves the cursor and request counter untouched.
    async with migrated_session_factory() as session:
        persisted = await session.get(UpstreamKeyRow, disabled.id)
    assert str(captured.value) == "no_eligible_upstream_key"
    assert persisted is not None
    assert persisted.request_count == 0
    assert await scheduler.current_cursor() is None
