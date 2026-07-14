import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    EventOutcome,
    EventType,
    HealthState,
    ProbeStatus,
    UpstreamKeyCreateRequest,
)
from nvidia_build_lb.credential_types import Clock, ResourceConflictError, ResourceNotFoundError
from nvidia_build_lb.db_models import AdminEventRow, SchedulerStateRow, UpstreamKeyRow
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


async def test_upstream_create_is_disabled_encrypted_and_decryptable_only_in_repository(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: a valid unchanged synthetic upstream credential.
    credential = " opaque-é-upstream "
    repository = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )

    # When: the repository creates its encrypted row.
    created = await repository.create(
        UpstreamKeyCreateRequest(key=credential),
        request_id="request-create",
    )

    # Then: the wire is secret-free, storage is encrypted, and internal decryption round-trips.
    expected = hashlib.sha256(credential.encode()).hexdigest()
    assert created.fingerprint == f"sha256:{expected}"
    assert created.enabled is False
    assert created.health_state is HealthState.UNKNOWN
    assert created.created_at == created.updated_at == fixed_clock.now()
    async with migrated_session_factory() as session:
        row = await session.get(UpstreamKeyRow, created.id)
        assert row is not None
        storage_corpus = row.fingerprint.encode() + row.vault_nonce + row.vault_ciphertext
        event = (
            await session.scalars(
                select(AdminEventRow).where(AdminEventRow.upstream_key_id == created.id)
            )
        ).one()
    assert credential.encode() not in storage_corpus
    assert event.event_type == EventType.UPSTREAM_KEY_CREATED.value
    assert event.outcome_class == EventOutcome.SUCCEEDED.value
    assert (await repository.secret_for(created.id)).get_secret_value() == credential


async def test_upstream_duplicate_fingerprint_is_a_conflict(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one persisted synthetic credential.
    repository = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    request = UpstreamKeyCreateRequest(key="duplicate-synthetic-key")
    _ = await repository.create(request, request_id="request-first")

    # When: the exact UTF-8 credential is created again.
    with pytest.raises(ResourceConflictError) as captured:
        _ = await repository.create(request, request_id="request-duplicate")

    # Then: the conflict is stable and no duplicate row or event commits.
    async with migrated_session_factory() as session:
        key_count = await session.scalar(select(func.count()).select_from(UpstreamKeyRow))
        event_count = await session.scalar(select(func.count()).select_from(AdminEventRow))
    assert str(captured.value) == "resource_conflict"
    assert key_count == 1
    assert event_count == 1


async def test_upstream_list_is_complete_safe_and_stably_ordered(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: two rows with the same creation timestamp and distinct random UUIDs.
    repository = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    _ = await repository.create(UpstreamKeyCreateRequest(key="synthetic-key-a"), request_id="a")
    _ = await repository.create(UpstreamKeyCreateRequest(key="synthetic-key-b"), request_id="b")

    # When: the complete unpaginated list is read.
    listed = await repository.list_all()

    # Then: all secret-free rows are ordered by created_at and UUID.
    assert len(listed.items) == 2
    order = tuple((item.created_at, item.id.int) for item in listed.items)
    assert order == tuple(sorted(order))
    assert all("synthetic-key" not in item.model_dump_json() for item in listed.items)


async def test_upstream_state_transitions_and_delete_are_exact(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one newly disabled key and one unknown identity.
    repository = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    created = await repository.create(
        UpstreamKeyCreateRequest(key="state-transition-key"),
        request_id="created",
    )
    unknown_id = uuid4()

    # When: enable is repeated, enabled delete is rejected, then disable and delete run.
    await repository.enable(created.id, request_id="enabled")
    await repository.enable(created.id, request_id="enabled-again")
    with pytest.raises(ResourceConflictError):
        await repository.delete(created.id, request_id="delete-enabled")
    await repository.disable(created.id, request_id="disabled")
    await repository.disable(created.id, request_id="disabled-again")
    await repository.delete(created.id, request_id="deleted")

    # Then: the row is gone and repeated/unknown operations are not found.
    with pytest.raises(ResourceNotFoundError):
        await repository.delete(created.id, request_id="deleted-again")
    with pytest.raises(ResourceNotFoundError):
        await repository.enable(unknown_id, request_id="unknown")
    async with migrated_session_factory() as session:
        assert await session.get(UpstreamKeyRow, created.id) is None


async def test_upstream_delete_locks_scheduler_before_key(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    repository = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    created = await repository.create(
        UpstreamKeyCreateRequest(key="delete-lock-order-key"),
        request_id="delete-lock-order-create",
    )
    async with migrated_session_factory() as probe_session:
        bind = probe_session.bind
    assert isinstance(bind, AsyncEngine)
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(bind.sync_engine, "before_cursor_execute", record_statement)
    try:
        await repository.delete(created.id, request_id="delete-lock-order")
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", record_statement)

    locking_tables = tuple(
        "scheduler" if "FROM scheduler_state" in statement else "upstream"
        for statement in statements
        if "FOR UPDATE" in statement
        and ("FROM scheduler_state" in statement or "FROM upstream_keys" in statement)
    )
    assert locking_tables[:2] == ("scheduler", "upstream")
    async with migrated_session_factory() as session:
        assert await session.get(SchedulerStateRow, 1) is not None


async def test_upstream_probe_projection_is_closed_and_preserves_enabled_state(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    # Given: one newly disabled key and a typed external probe observation.
    repository = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    created = await repository.create(
        UpstreamKeyCreateRequest(key="probe-projection-key"),
        request_id="probe-projection-create",
    )

    # When: the repository projects the safe result after external I/O.
    projected = await repository.probe_result(created.id, ProbeStatus.VALID)

    # Then: only the row identity, current enabled flag, status, and clock are returned.
    assert projected.id == created.id
    assert projected.enabled is False
    assert projected.probe_status is ProbeStatus.VALID
    assert projected.observed_at == fixed_clock.now()
    assert set(projected.model_dump()) == {"id", "enabled", "probe_status", "observed_at"}
