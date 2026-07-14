from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import create_async_engine
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
from nvidia_build_lb.schemas import ErrorEnvelope
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

from .conftest import ADMIN_TOKEN

pytestmark = pytest.mark.vault_auth

_HOST = "127.0.0.1:2456"


@dataclass(frozen=True, slots=True)
class _RepositoryProbe:
    upstream: UpstreamKeyRepository

    async def probe(self, key_id: UUID, request_id: str) -> UpstreamProbeResponse:
        del request_id
        return await self.upstream.probe_result(key_id, ProbeStatus.VALID)


@contextmanager
def _unavailable_client(clock: Clock, vault: Vault) -> Generator[TestClient]:
    engine = create_async_engine(
        "postgresql+asyncpg://synthetic:synthetic@127.0.0.1:1/synthetic",
        poolclass=NullPool,
        pool_pre_ping=True,
    )
    sessions = create_session_factory(engine)
    upstream = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock))
    downstream = DownstreamTokenRepository(
        DownstreamTokenDependencies(sessions, clock, SystemTokenHexSource())
    )
    services = CredentialServices(
        CredentialRepositories(upstream, downstream, sessions, clock),
        CredentialAuthenticators(
            AdminAuthenticator(SecretStr(ADMIN_TOKEN)),
            DownstreamAuthenticator(downstream),
        ),
        _RepositoryProbe(upstream),
    )
    try:
        with TestClient(
            create_credential_test_app(services),
            base_url=f"http://{_HOST}",
        ) as client:
            yield client
    finally:
        anyio.run(engine.dispose)


def test_http_admin_database_failure_is_safe_503(
    fixed_clock: Clock,
    vault: Vault,
) -> None:
    # Given: an authenticated administration slice whose PostgreSQL endpoint is unavailable.
    headers = {"Host": _HOST, "Authorization": f"Bearer {ADMIN_TOKEN}"}

    # When: the repository-backed list route attempts its first connection.
    with _unavailable_client(fixed_clock, vault) as client:
        response = client.get("/admin/api/v1/upstream-keys", headers=headers)

    # Then: the response is the locked safe database error with no DSN material.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 503
    assert error.error.code == "database_unavailable"
    assert "synthetic" not in response.text
    assert "127.0.0.1:1" not in response.text


def test_http_wrong_admin_still_rejects_before_unavailable_database(
    fixed_clock: Clock,
    vault: Vault,
) -> None:
    # Given: an unavailable database and a shape-valid wrong admin bearer.
    wrong = f"nblb_admin_{'f' * 64}"
    headers = {"Host": _HOST, "Authorization": f"Bearer {wrong}"}

    # When: the request reaches the ordered authentication boundary.
    with _unavailable_client(fixed_clock, vault) as client:
        response = client.get("/admin/api/v1/upstream-keys", headers=headers)

    # Then: admin authentication wins without attempting to classify the database.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 401
    assert error.error.code == "admin_unauthorized"
    assert response.headers["www-authenticate"] == 'Bearer realm="nvidia-build-lb-admin"'


def test_http_downstream_database_failure_is_safe_503(
    fixed_clock: Clock,
    vault: Vault,
) -> None:
    # Given: a shape-valid downstream bearer and unavailable PostgreSQL endpoint.
    token = f"nblb_ds_{'b' * 64}"
    headers = {"Host": _HOST, "Authorization": f"Bearer {token}"}

    # When: authentication attempts its digest lookup.
    with _unavailable_client(fixed_clock, vault) as client:
        response = client.get("/v1/models", headers=headers)

    # Then: infrastructure failure is distinct from invalid authentication.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 503
    assert error.error.code == "database_unavailable"
    assert "www-authenticate" not in response.headers
