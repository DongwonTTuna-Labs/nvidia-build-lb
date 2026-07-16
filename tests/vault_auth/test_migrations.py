from __future__ import annotations

from typing import TYPE_CHECKING

import anyio
import pytest
from sqlalchemy import String, column, inspect, select, table
from sqlalchemy.ext.asyncio import create_async_engine

from .conftest import alembic_config, downgrade, migrate

if TYPE_CHECKING:
    from alembic.config import Config
    from pydantic import SecretStr

pytestmark = pytest.mark.vault_auth

_DOMAIN_TABLES = {
    "admin_events",
    "admin_ledger_state",
    "downstream_tokens",
    "scheduler_state",
    "upstream_attempt_receipts",
    "upstream_live_pins",
    "upstream_keys",
    "vault_key_verifier",
}


async def _schema_snapshot(database_url: SecretStr) -> tuple[str, frozenset[str]]:
    engine = create_async_engine(database_url.get_secret_value(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            revision = await connection.scalar(
                select(column("version_num", String)).select_from(table("alembic_version"))
            )
            tables = await connection.run_sync(
                lambda sync: frozenset(inspect(sync).get_table_names())
            )
    finally:
        await engine.dispose()
    assert isinstance(revision, str)
    return revision, tables


async def _storage_contract(database_url: SecretStr) -> tuple[dict[str, frozenset[str]], set[str]]:
    engine = create_async_engine(database_url.get_secret_value(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync: {
                    table: frozenset(column["name"] for column in inspect(sync).get_columns(table))
                    for table in _DOMAIN_TABLES
                }
            )
            unique_columns = await connection.run_sync(
                lambda sync: {
                    column
                    for table in ("upstream_keys", "downstream_tokens")
                    for constraint in inspect(sync).get_unique_constraints(table)
                    for column in constraint["column_names"]
                }
            )
    finally:
        await engine.dispose()
    return columns, unique_columns


def test_empty_database_upgrades_to_credential_head(empty_database: SecretStr) -> None:
    # Given: a real PostgreSQL 17 database with an empty public schema.
    config = alembic_config(empty_database)

    # When: every migration through the current head is applied.
    migrate(config, "head")

    # Then: the domain head and all credential and verifier tables exist.
    revision, tables = anyio.run(_schema_snapshot, empty_database)
    assert revision == "0005_admin_dashboard_ledger"
    assert tables >= _DOMAIN_TABLES


def test_credential_migration_round_trips_through_baseline(empty_database: SecretStr) -> None:
    # Given: a real PostgreSQL 17 database upgraded to the credential head.
    config: Config = alembic_config(empty_database)
    migrate(config, "0005_admin_dashboard_ledger")

    # When: the domain migration is downgraded and upgraded once.
    downgrade(config, "0001_baseline")
    baseline_revision, baseline_tables = anyio.run(_schema_snapshot, empty_database)
    migrate(config, "0005_admin_dashboard_ledger")

    # Then: downgrade removes only domain state and the upgrade restores the head.
    head_revision, head_tables = anyio.run(_schema_snapshot, empty_database)
    assert baseline_revision == "0001_baseline"
    assert _DOMAIN_TABLES.isdisjoint(baseline_tables)
    assert head_revision == "0005_admin_dashboard_ledger"
    assert head_tables >= _DOMAIN_TABLES


def test_credential_schema_has_only_encrypted_or_digest_secret_storage(
    empty_database: SecretStr,
) -> None:
    # Given: an empty real PostgreSQL 17 database.
    config = alembic_config(empty_database)

    # When: the credential schema is migrated to its head.
    migrate(config, "0005_admin_dashboard_ledger")

    # Then: secret columns are encrypted or digests and critical identities are unique.
    columns, unique_columns = anyio.run(_storage_contract, empty_database)
    assert {"vault_nonce", "vault_ciphertext", "fingerprint"} <= columns["upstream_keys"]
    assert {"label_bytes", "token_digest"} <= columns["downstream_tokens"]
    assert {"key", "plaintext", "token"}.isdisjoint(
        columns["upstream_keys"] | columns["downstream_tokens"]
    )
    assert {"fingerprint", "label_bytes", "token_digest"} <= unique_columns
