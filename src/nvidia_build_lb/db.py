"""Lazy SQLAlchemy async engine and session construction."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared migration metadata; domain models land in the vault todo."""


def create_engine(database_url: SecretStr) -> AsyncEngine:
    """Create a lazy async engine without importing process settings."""
    return create_async_engine(
        database_url.get_secret_value(),
        echo=False,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create sessions whose loaded attributes remain usable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """Yield one explicitly scoped async database session."""
    async with session_factory() as session:
        yield session


@asynccontextmanager
async def transaction_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncSession]:
    """Yield one session whose successful scope commits atomically."""
    async with session_factory.begin() as session:
        yield session
