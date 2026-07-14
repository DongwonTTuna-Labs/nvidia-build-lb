from datetime import UTC, datetime
from uuid import uuid4

import anyio
import pytest
from pydantic import SecretStr
from sqlalchemy import String, column, inspect, select, table, text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.vault_auth.conftest import alembic_config, downgrade, migrate

pytestmark = pytest.mark.nvidia_routing


async def _schema(database_url: SecretStr) -> dict[str, set[str]]:
    engine = create_async_engine(database_url.get_secret_value())
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: {
                    table: {column["name"] for column in inspect(sync).get_columns(table)}
                    for table in inspect(sync).get_table_names()
                }
            )
    finally:
        await engine.dispose()


def test_empty_upgrade_has_exact_routing_tables_and_columns(empty_database: SecretStr) -> None:
    migrate(alembic_config(empty_database), "0003_nvidia_routing")
    schema = anyio.run(_schema, empty_database)
    assert schema["upstream_attempt_receipts"] == {
        "started_event_id",
        "terminal_event_id",
        "upstream_key_id",
        "request_id",
        "service_epoch",
        "explicit_probe_key_id",
        "excluded_key_ids",
        "started_at",
        "rate_limit_streak",
        "transient_failure_streak",
        "terminal_outcome",
        "terminal_status_class",
        "terminal_latency_ms",
        "terminal_cooldown_until",
        "terminal_cooldown_kind",
        "terminal_committed_at",
    }
    assert schema["upstream_live_pins"] == {
        "started_event_id",
        "upstream_key_id",
        "service_epoch",
        "pinned_at",
    }
    assert "cooldown_kind" in schema["upstream_keys"]
    assert "attempt_started_event_id" in schema["admin_events"]


def test_populated_0002_upgrade_classifies_legacy_cooldown_as_transient(
    empty_database: SecretStr,
) -> None:
    config = alembic_config(empty_database)
    migrate(config, "0002_vault_auth")

    async def seed() -> None:
        engine = create_async_engine(empty_database.get_secret_value())
        try:
            async with engine.begin() as connection:
                _ = await connection.execute(
                    text(
                        """
                        INSERT INTO upstream_keys (
                            id,
                            fingerprint,
                            vault_version,
                            vault_nonce,
                            vault_ciphertext,
                            enabled,
                            health_state,
                            cooldown_until,
                            quarantined,
                            request_count,
                            success_count,
                            failure_count,
                            consecutive_rate_limits,
                            consecutive_transient_failures,
                            created_at,
                            updated_at
                        ) VALUES (
                            :id,
                            :fingerprint,
                            1,
                            :nonce,
                            :ciphertext,
                            true,
                            'degraded',
                            :cooldown,
                            false,
                            0,
                            0,
                            0,
                            0,
                            0,
                            :now,
                            :now
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "fingerprint": "a" * 64,
                        "nonce": b"n" * 12,
                        "ciphertext": b"c" * 17,
                        "cooldown": datetime(2026, 1, 1, tzinfo=UTC),
                        "now": datetime(2025, 1, 1, tzinfo=UTC),
                    },
                )
        finally:
            await engine.dispose()

    anyio.run(seed)
    migrate(config, "0003_nvidia_routing")

    async def read_kind() -> str | None:
        engine = create_async_engine(empty_database.get_secret_value())
        try:
            async with engine.connect() as connection:
                return await connection.scalar(
                    select(column("cooldown_kind", String())).select_from(table("upstream_keys"))
                )
        finally:
            await engine.dispose()

    assert anyio.run(read_kind) == "transient"


def test_downgrade_and_reupgrade_restore_exact_schema(empty_database: SecretStr) -> None:
    config = alembic_config(empty_database)
    migrate(config, "0003_nvidia_routing")
    first = anyio.run(_schema, empty_database)
    downgrade(config, "0002_vault_auth")
    middle = anyio.run(_schema, empty_database)
    assert "upstream_attempt_receipts" not in middle
    assert "upstream_live_pins" not in middle
    assert "cooldown_kind" not in middle["upstream_keys"]
    assert "attempt_started_event_id" not in middle["admin_events"]
    migrate(config, "0003_nvidia_routing")
    assert anyio.run(_schema, empty_database) == first
