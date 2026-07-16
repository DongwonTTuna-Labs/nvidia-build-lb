"""Alembic migration environment for the async PostgreSQL schema."""

from typing import Final

import anyio
from alembic import context
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from nvidia_build_lb.db_models import DOMAIN_METADATA

_CONFIG = context.config
_URL_OPTION: Final = "sqlalchemy.url"
_MIGRATION_LOCK_KEY_ONE: Final = 1_312_967_746
_MIGRATION_LOCK_KEY_TWO: Final = 2
_RUNTIME_LOCK_KEY_TWO: Final = 1
_MIGRATION_LOCK_TIMEOUT_MS: Final = 5_000
_MIGRATION_STATEMENT_TIMEOUT_MS: Final = 120_000
_RUNTIME_LOCK_UNAVAILABLE: Final = "runtime_lock_unavailable"


def run_migrations_offline() -> None:
    """Render migrations without opening a database connection."""
    context.configure(
        url=_CONFIG.get_main_option(_URL_OPTION),
        target_metadata=DOMAIN_METADATA,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    migration_acquired = False
    runtime_acquired = False
    try:
        _ = connection.execute(
            text("SELECT set_config('statement_timeout', :timeout_ms, false)"),
            {"timeout_ms": str(_MIGRATION_STATEMENT_TIMEOUT_MS)},
        )
        _ = connection.execute(
            text("SELECT set_config('lock_timeout', :timeout_ms, false)"),
            {"timeout_ms": str(_MIGRATION_LOCK_TIMEOUT_MS)},
        )
        _ = connection.execute(
            text("SELECT pg_advisory_lock(:first_key, :second_key)"),
            {
                "first_key": _MIGRATION_LOCK_KEY_ONE,
                "second_key": _MIGRATION_LOCK_KEY_TWO,
            },
        )
        migration_acquired = True
        runtime_acquired = (
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:first_key, :second_key)"),
                {
                    "first_key": _MIGRATION_LOCK_KEY_ONE,
                    "second_key": _RUNTIME_LOCK_KEY_TWO,
                },
            )
            is True
        )
        if not runtime_acquired:
            raise RuntimeError(_RUNTIME_LOCK_UNAVAILABLE)
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=DOMAIN_METADATA,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if runtime_acquired:
            _ = connection.execute(
                text("SELECT pg_advisory_unlock(:first_key, :second_key)"),
                {
                    "first_key": _MIGRATION_LOCK_KEY_ONE,
                    "second_key": _RUNTIME_LOCK_KEY_TWO,
                },
            )
        if migration_acquired:
            _ = connection.execute(
                text("SELECT pg_advisory_unlock(:first_key, :second_key)"),
                {
                    "first_key": _MIGRATION_LOCK_KEY_ONE,
                    "second_key": _MIGRATION_LOCK_KEY_TWO,
                },
            )
            connection.commit()


async def _run_migrations_online() -> None:
    section = _CONFIG.get_section(_CONFIG.config_ini_section) or {}
    engine = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    """Apply migrations through one explicitly disposed async engine."""
    anyio.run(_run_migrations_online)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
