"""Migration 0005 lock, rollback, compatibility, and schema sensors."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import anyio
import psycopg
import pytest
from sqlalchemy import String, column, func, inspect, select, table, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from nvidia_build_lb.db import create_session_factory
from nvidia_build_lb.db_models import AdminEventRow
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository

from .conftest import alembic_config, migrate

if TYPE_CHECKING:
    from pydantic import SecretStr
    from sqlalchemy.sql import Executable

    from nvidia_build_lb.credential_types import Clock
    from nvidia_build_lb.vault import Vault

pytestmark = pytest.mark.vault_auth

_LOCK_KEY_ONE = 1_312_967_746
_RUNTIME_LOCK_KEY_TWO = 1


class _InjectedPreflightTimeoutError(SqlAlchemyTimeoutError):
    """Deterministic migration preflight timeout sensor."""


def _sync_dsn(database_url: SecretStr) -> str:
    return (
        make_url(database_url.get_secret_value())
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


async def _revision(database_url: SecretStr) -> str:
    engine = create_async_engine(database_url.get_secret_value())
    try:
        async with engine.connect() as connection:
            value = await connection.scalar(
                select(column("version_num", String)).select_from(table("alembic_version"))
            )
    finally:
        await engine.dispose()
    assert isinstance(value, str)
    return value


async def _head_schema(database_url: SecretStr) -> tuple[set[str], set[str]]:
    engine = create_async_engine(database_url.get_secret_value())
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))
            event_columns = await connection.run_sync(
                lambda sync: {item["name"] for item in inspect(sync).get_columns("admin_events")}
            )
    finally:
        await engine.dispose()
    return tables, event_columns


def test_migration_refuses_live_service_epoch_without_schema_change(
    empty_database: SecretStr,
) -> None:
    config = alembic_config(empty_database)
    migrate(config, "0004_vault_key_verifier")

    with psycopg.connect(_sync_dsn(empty_database), autocommit=True) as connection:
        _ = connection.execute(
            "SELECT pg_advisory_lock(%s, %s)",
            (_LOCK_KEY_ONE, _RUNTIME_LOCK_KEY_TWO),
        ).fetchone()
        with pytest.raises(RuntimeError, match="runtime_lock_unavailable"):
            migrate(config, "0005_admin_dashboard_ledger")

    assert anyio.run(_revision, empty_database) == "0004_vault_key_verifier"
    tables, event_columns = anyio.run(_head_schema, empty_database)
    assert "admin_ledger_state" not in tables
    assert "writer_generation" not in event_columns
    assert "upstream_key_fingerprint" not in event_columns


def test_migration_preflight_timeout_rolls_back_0005(
    empty_database: SecretStr,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = alembic_config(empty_database)
    migrate(config, "0004_vault_key_verifier")

    def _timeout_preflight(
        _connection: Connection,
        statement: Executable,
        *_args: object,
        **_kwargs: object,
    ) -> object:
        rendered = str(statement)
        if "count(" in rendered and "FROM admin_events" in rendered:
            raise _InjectedPreflightTimeoutError
        return True

    monkeypatch.setattr(Connection, "scalar", _timeout_preflight)
    with pytest.raises(_InjectedPreflightTimeoutError):
        migrate(config, "0005_admin_dashboard_ledger")
    monkeypatch.undo()

    assert anyio.run(_revision, empty_database) == "0004_vault_key_verifier"
    tables, event_columns = anyio.run(_head_schema, empty_database)
    assert "admin_ledger_state" not in tables
    assert "writer_generation" not in event_columns
    assert "upstream_key_fingerprint" not in event_columns


def test_prior_image_event_write_is_rejected_by_writer_generation(
    empty_database: SecretStr,
) -> None:
    migrate(alembic_config(empty_database), "0005_admin_dashboard_ledger")

    async def _write_old_shape() -> int:
        engine = create_async_engine(empty_database.get_secret_value())
        try:
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    _ = await connection.execute(
                        text(
                            """
                            INSERT INTO admin_events (
                                id, request_id, event_type, upstream_key_id,
                                downstream_token_id, outcome_class, status_class,
                                latency_ms, occurred_at, attempt_started_event_id
                            ) VALUES (
                                :id, 'legacy-writer', 'downstream_token_issued', NULL,
                                NULL, 'succeeded', NULL, NULL, CURRENT_TIMESTAMP, NULL
                            )
                            """
                        ),
                        {"id": uuid4()},
                    )
            async with engine.connect() as connection:
                count = await connection.scalar(
                    select(func.count()).select_from(table("admin_events"))
                )
        finally:
            await engine.dispose()
        assert isinstance(count, int)
        return count

    assert anyio.run(_write_old_shape) == 0


def test_empty_0004_upgrade_adds_exact_dashboard_ledger_schema(
    empty_database: SecretStr,
) -> None:
    migrate(alembic_config(empty_database), "0005_admin_dashboard_ledger")
    tables, event_columns = anyio.run(_head_schema, empty_database)
    assert "admin_ledger_state" in tables
    assert {"upstream_key_fingerprint", "writer_generation"} <= event_columns


async def _seed_legacy_key_event(
    database_url: SecretStr,
    vault: Vault,
    clock: Clock,
    identity: tuple[UUID, UUID],
    fingerprint: str,
) -> None:
    key_id, event_id = identity
    envelope = vault.encrypt(key_id, "legacy-materialization-key")
    engine = create_async_engine(database_url.get_secret_value(), poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            _ = await connection.execute(
                text(
                    """
                    INSERT INTO upstream_keys (
                        id, fingerprint, vault_version, vault_nonce, vault_ciphertext,
                        enabled, health_state, cooldown_until, cooldown_kind, quarantined,
                        request_count, success_count, failure_count, consecutive_rate_limits,
                        consecutive_transient_failures, last_status_class, last_used_at,
                        created_at, updated_at
                    ) VALUES (
                        :id, :fingerprint, :vault_version, :vault_nonce, :vault_ciphertext,
                        false, 'unknown', NULL, NULL, false,
                        0, 0, 0, 0, 0, NULL, NULL, :now, :now
                    )
                    """
                ),
                {
                    "id": key_id,
                    "fingerprint": fingerprint,
                    "vault_version": envelope.version,
                    "vault_nonce": envelope.nonce,
                    "vault_ciphertext": envelope.ciphertext,
                    "now": clock.now(),
                },
            )
            _ = await connection.execute(
                text(
                    """
                    INSERT INTO admin_events (
                        id, request_id, event_type, upstream_key_id,
                        downstream_token_id, outcome_class, status_class,
                        latency_ms, occurred_at, attempt_started_event_id
                    ) VALUES (
                        :id, 'legacy-event', 'upstream_key_disabled', :key_id,
                        NULL, 'succeeded', NULL, NULL, :now, NULL
                    )
                    """
                ),
                {"id": event_id, "key_id": key_id, "now": clock.now()},
            )
    finally:
        await engine.dispose()


async def _delete_and_read_materialized_identity(
    database_url: SecretStr,
    vault: Vault,
    clock: Clock,
    key_id: UUID,
    event_id: UUID,
) -> tuple[AdminEventRow | None, AdminEventRow | None]:
    engine = create_async_engine(database_url.get_secret_value(), poolclass=NullPool)
    sessions = create_session_factory(engine)
    try:
        repository = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock))
        await repository.delete(key_id, request_id="delete-after-migration")
        async with sessions() as session:
            legacy = await session.get(AdminEventRow, event_id)
            deleted = await session.scalar(
                select(AdminEventRow).where(AdminEventRow.request_id == "delete-after-migration")
            )
        return legacy, deleted
    finally:
        await engine.dispose()


def test_delete_materializes_same_key_legacy_event_identity_atomically(
    empty_database: SecretStr,
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = uuid4()
    legacy_event_id = uuid4()
    fingerprint = "f" * 64
    config = alembic_config(empty_database)
    migrate(config, "0004_vault_key_verifier")
    anyio.run(
        _seed_legacy_key_event,
        empty_database,
        vault,
        fixed_clock,
        (key_id, legacy_event_id),
        fingerprint,
    )
    migrate(config, "0005_admin_dashboard_ledger")

    legacy, deleted = anyio.run(
        _delete_and_read_materialized_identity,
        empty_database,
        vault,
        fixed_clock,
        key_id,
        legacy_event_id,
    )
    assert legacy is not None
    assert legacy.upstream_key_fingerprint == fingerprint
    assert legacy.writer_generation == 5
    assert deleted is not None
    assert deleted.upstream_key_fingerprint == fingerprint
    assert deleted.writer_generation == 5
