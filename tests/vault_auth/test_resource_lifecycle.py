import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


async def test_repository_fixture_retains_no_event_loop_bound_connections(
    migrated_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Given: the real PostgreSQL repository fixture's session factory.
    async with migrated_session_factory() as session:
        # When: its engine pool policy is inspected without opening a connection.
        bind = session.bind

    # Then: function-scoped QA cannot retain asyncpg transports across AnyIO loops.
    assert isinstance(bind, AsyncEngine)
    assert isinstance(bind.pool, NullPool)
