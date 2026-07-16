from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol, Self, runtime_checkable
from uuid import UUID, uuid4

import anyio
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretBytes, SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from nvidia_build_lb.admin.schemas import ProbeStatus, UpstreamProbeResponse
from nvidia_build_lb.admin_credentials import (
    CredentialRepositories,
    CredentialServices,
    create_credential_test_app,
)
from nvidia_build_lb.auth import (
    AdminAuthenticator,
    CredentialAuthenticators,
    DownstreamAuthenticator,
)
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db import create_session_factory
from nvidia_build_lb.downstream_tokens import (
    DownstreamTokenDependencies,
    DownstreamTokenRepository,
    SystemTokenHexSource,
)
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

ADMIN_TOKEN = f"nblb_admin_{'a' * 64}"


class _PostgresContainer(Protocol):
    def with_name(self, name: str) -> Self: ...

    def with_kwargs(self, *, labels: dict[str, str]) -> Self: ...

    def start(self) -> Self: ...

    def stop(self) -> None: ...

    def get_connection_url(self) -> str: ...


class _PostgresContainerFactory(Protocol):
    def __call__(self, image: str) -> _PostgresContainer: ...


class _ReaperController(Protocol):
    def delete_instance(self) -> None: ...


@runtime_checkable
class _PostgresModule(Protocol):
    PostgresContainer: _PostgresContainerFactory


@runtime_checkable
class _TestcontainersCoreModule(Protocol):
    Reaper: _ReaperController


def _postgres_container_factory() -> _PostgresContainerFactory:
    module = import_module("testcontainers.postgres")
    assert isinstance(module, _PostgresModule)
    return module.PostgresContainer


def _delete_testcontainers_reaper() -> None:
    module = import_module("testcontainers.core.container")
    assert isinstance(module, _TestcontainersCoreModule)
    module.Reaper.delete_instance()


@dataclass(frozen=True, slots=True)
class FixedClock:
    timestamp: datetime

    def now(self) -> datetime:
        return self.timestamp


@dataclass(frozen=True, slots=True)
class CredentialHttpSlice:
    client: TestClient
    repositories: CredentialRepositories


@dataclass(frozen=True, slots=True)
class _ValidProbe:
    upstream: UpstreamKeyRepository

    async def probe(self, key_id: UUID, request_id: str) -> UpstreamProbeResponse:
        del request_id
        return await self.upstream.probe_result(key_id, ProbeStatus.VALID)


def _async_dsn(raw_url: str) -> SecretStr:
    url = make_url(raw_url).set(drivername="postgresql+asyncpg")
    return SecretStr(url.render_as_string(hide_password=False))


async def _reset_public_schema(database_url: SecretStr) -> None:
    engine = create_async_engine(database_url.get_secret_value(), pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            _ = await connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            _ = await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def postgres_database_url() -> Generator[SecretStr]:
    container_name = f"nvidia-build-lb-todo2-{uuid4().hex[:12]}"
    container = (
        _postgres_container_factory()("postgres:17-alpine")
        .with_name(container_name)
        .with_kwargs(labels={"nvidia-build-lb.task": "todo2-vault-auth"})
    )
    try:
        _ = container.start()
        yield _async_dsn(container.get_connection_url())
    finally:
        try:
            container.stop()
        finally:
            _delete_testcontainers_reaper()


@pytest.fixture
def empty_database(postgres_database_url: SecretStr) -> Generator[SecretStr]:
    anyio.run(_reset_public_schema, postgres_database_url)
    try:
        yield postgres_database_url
    finally:
        anyio.run(_reset_public_schema, postgres_database_url)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def fixed_clock() -> Clock:
    return FixedClock(timestamp=datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def vault() -> Vault:
    return Vault(SecretBytes(b"v" * 32))


@pytest.fixture
def migrated_session_factory(
    empty_database: SecretStr,
) -> Generator[async_sessionmaker[AsyncSession]]:
    migrate(alembic_config(empty_database), "0005_admin_dashboard_ledger")
    engine = create_async_engine(
        empty_database.get_secret_value(),
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    try:
        yield create_session_factory(engine)
    finally:
        anyio.run(engine.dispose)


@pytest.fixture
def credential_http_slice(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> Generator[CredentialHttpSlice]:
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(migrated_session_factory, vault, fixed_clock)
    )
    downstream = DownstreamTokenRepository(
        DownstreamTokenDependencies(
            migrated_session_factory,
            fixed_clock,
            SystemTokenHexSource(),
        )
    )
    repositories = CredentialRepositories(
        upstream,
        downstream,
        migrated_session_factory,
        fixed_clock,
    )
    authenticators = CredentialAuthenticators(
        AdminAuthenticator(SecretStr(ADMIN_TOKEN)),
        DownstreamAuthenticator(downstream),
    )
    app = create_credential_test_app(
        CredentialServices(repositories, authenticators, _ValidProbe(upstream))
    )
    with TestClient(app, base_url="http://127.0.0.1:2456") as client:
        yield CredentialHttpSlice(client, repositories)


def alembic_config(database_url: SecretStr) -> Config:
    config = Config(Path("alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.get_secret_value())
    return config


def migrate(config: Config, revision: str) -> None:
    command.upgrade(config, revision)


def downgrade(config: Config, revision: str) -> None:
    command.downgrade(config, revision)
