from collections.abc import Generator

import anyio
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from nvidia_build_lb.db import create_session_factory
from tests.vault_auth.conftest import (
    alembic_config,
    anyio_backend,
    empty_database,
    fixed_clock,
    migrate,
    postgres_database_url,
    vault,
)

__all__ = [
    "anyio_backend",
    "empty_database",
    "fixed_clock",
    "postgres_database_url",
    "routing_session_factory",
    "vault",
]


@pytest.fixture
def routing_session_factory(
    empty_database: SecretStr,
) -> Generator[async_sessionmaker[AsyncSession]]:
    migrate(alembic_config(empty_database), "0004_vault_key_verifier")
    engine = create_async_engine(
        empty_database.get_secret_value(),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        yield create_session_factory(engine)
    finally:
        anyio.run(engine.dispose)
