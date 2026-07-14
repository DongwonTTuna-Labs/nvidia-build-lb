from collections import Counter
from datetime import timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import LastStatusClass, UpstreamKeyCreateRequest
from nvidia_build_lb.credential_types import Clock, ResourceConflictError
from nvidia_build_lb.db_models import UpstreamKeyRow
from nvidia_build_lb.scheduler_state import (
    AttemptTerminal,
    SchedulerDependencies,
    SchedulerStateRepository,
    TerminalOutcome,
)
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


async def _create_enabled_key(
    repository: UpstreamKeyRepository,
    credential: str,
) -> UUID:
    created = await repository.create(
        UpstreamKeyCreateRequest(key=credential),
        request_id=f"create-{credential}",
    )
    await repository.enable(created.id, request_id=f"enable-{credential}")
    return created.id


async def test_next_attempt_selection_is_atomic_and_fair_under_concurrency(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: two equally eligible keys and one persisted scheduler cursor.
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    key_ids = {
        await _create_enabled_key(upstream, "concurrent-key-a"),
        await _create_enabled_key(upstream, "concurrent-key-b"),
    }
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    selected: list[UUID] = []

    async def reserve(request_id: str) -> None:
        lease = await scheduler.begin_next_attempt(request_id)
        selected.append(lease.key_id)

    # When: four public attempts reserve keys concurrently.
    async with anyio.create_task_group() as tasks:
        for index in range(4):
            _ = tasks.start_soon(reserve, f"concurrent-{index}")

    # Then: the database lock serializes cursor advancement into a two-two split.
    counts = Counter(selected)
    assert set(counts) == key_ids
    assert set(counts.values()) == {2}


async def test_rate_limit_terminal_updates_accumulate_without_shortening_cooldown(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: two committed attempts for one key and two rate-limit observations.
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    key_id = await _create_enabled_key(upstream, "rate-limit-key")
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    first = await scheduler.begin_attempt(key_id, "rate-first")
    second = await scheduler.begin_attempt(key_id, "rate-second")
    short = AttemptTerminal(
        outcome=TerminalOutcome.FAILED,
        status_class=LastStatusClass.RATE_LIMITED,
        latency_ms=10,
        cooldown_until=fixed_clock.now() + timedelta(seconds=30),
    )
    long = AttemptTerminal(
        outcome=TerminalOutcome.FAILED,
        status_class=LastStatusClass.RATE_LIMITED,
        latency_ms=11,
        cooldown_until=fixed_clock.now() + timedelta(seconds=60),
    )

    # When: both terminal transactions contend on the same row.
    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(scheduler.finish_attempt, first, short)
        _ = tasks.start_soon(scheduler.finish_attempt, second, long)

    # Then: both deltas persist and the later transaction cannot shorten cooldown.
    async with migrated_session_factory() as session:
        row = await session.get(UpstreamKeyRow, key_id)
    assert row is not None
    assert row.failure_count == 2
    assert row.consecutive_rate_limits == 2
    assert row.cooldown_until == long.cooldown_until


async def test_transient_terminal_updates_accumulate_under_row_lock(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: two committed attempts for one key and the same transient transition.
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    key_id = await _create_enabled_key(upstream, "transient-key")
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    first = await scheduler.begin_attempt(key_id, "transient-first")
    second = await scheduler.begin_attempt(key_id, "transient-second")
    terminal = AttemptTerminal(
        outcome=TerminalOutcome.FAILED,
        status_class=LastStatusClass.UPSTREAM_UNAVAILABLE,
        latency_ms=12,
        cooldown_until=fixed_clock.now() + timedelta(seconds=15),
    )

    # When: both terminal transactions contend on the same row.
    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(scheduler.finish_attempt, first, terminal)
        _ = tasks.start_soon(scheduler.finish_attempt, second, terminal)

    # Then: neither terminal overwrites the other's durable failure delta.
    async with migrated_session_factory() as session:
        row = await session.get(UpstreamKeyRow, key_id)
    assert row is not None
    assert row.failure_count == 2
    assert row.consecutive_transient_failures == 2


async def test_terminal_rejects_a_forged_started_event_identity(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one real pre-network lease whose event proof is replaced.
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    key_id = await _create_enabled_key(upstream, "forged-lease-key")
    scheduler = SchedulerStateRepository(
        SchedulerDependencies(migrated_session_factory, fixed_clock)
    )
    real = await scheduler.begin_attempt(key_id, "forged-lease-request")
    forged = type(real)(
        key_id=real.key_id,
        request_id=real.request_id,
        started_event_id=uuid4(),
        started_at=real.started_at,
        explicit_probe=real.explicit_probe,
    )
    terminal = AttemptTerminal(
        outcome=TerminalOutcome.SUCCEEDED,
        status_class=LastStatusClass.SUCCESS,
        latency_ms=1,
        cooldown_until=None,
    )

    # When: terminal completion presents the forged lease.
    with pytest.raises(ResourceConflictError):
        await scheduler.finish_attempt(forged, terminal)

    # Then: the real started event remains an uncompleted durable crash gap.
    async with migrated_session_factory() as session:
        row = await session.get(UpstreamKeyRow, key_id)
    assert row is not None
    assert row.request_count == 1
    assert row.success_count == row.failure_count == 0
