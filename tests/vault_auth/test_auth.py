from hashlib import sha256

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import DownstreamScope, DownstreamTokenIssueRequest
from nvidia_build_lb.auth import AdminAuthenticator, DownstreamAuthenticator
from nvidia_build_lb.credential_types import (
    AuthenticationRejectedError,
    AuthRealm,
    Clock,
    InsufficientScopeError,
)
from nvidia_build_lb.db_models import DownstreamTokenRow
from nvidia_build_lb.downstream_tokens import (
    DownstreamTokenDependencies,
    DownstreamTokenRepository,
    SystemTokenHexSource,
)

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


def _authorization(token: str) -> tuple[bytes, ...]:
    return (f"Bearer {token}".encode(),)


async def _issued_models_token(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> tuple[DownstreamTokenRepository, str]:
    repository = DownstreamTokenRepository(
        DownstreamTokenDependencies(sessions, clock, SystemTokenHexSource())
    )
    issued = await repository.issue(
        DownstreamTokenIssueRequest(
            label="auth-models-token", scopes=(DownstreamScope.MODELS_READ,)
        ),
        request_id="issue-models",
    )
    return repository, issued.token


async def test_admin_authenticator_accepts_only_exact_admin_realm_bearer() -> None:
    # Given: one synthetic admin bearer and realm-confusable alternatives.
    token = f"nblb_admin_{'a' * 64}"
    authenticator = AdminAuthenticator(SecretStr(token))
    rejected = (
        (),
        _authorization(f"nblb_admin_{'b' * 64}"),
        _authorization(f"nblb_ds_{'a' * 64}"),
        (f"Bearer {token}".encode(), f"Bearer {token}".encode()),
        (f"Bearer {token}, Bearer {token}".encode(),),
    )

    # When: the exact bearer and every confusable shape are authenticated.
    authenticator.authenticate(_authorization(token))
    failures: list[AuthRealm] = []
    for headers in rejected:
        with pytest.raises(AuthenticationRejectedError) as captured:
            authenticator.authenticate(headers)
        failures.append(captured.value.realm)

    # Then: only the exact singleton header succeeds and every failure stays in admin realm.
    assert failures == [AuthRealm.ADMIN] * len(rejected)


async def test_downstream_scope_success_commits_counter_before_return(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    fixed_clock: Clock,
) -> None:
    # Given: one valid models-only token.
    repository, token = await _issued_models_token(migrated_session_factory, fixed_clock)
    authenticator = DownstreamAuthenticator(repository)

    # When: models scope succeeds and chat scope is subsequently rejected.
    principal = await authenticator.authenticate(
        _authorization(token),
        DownstreamScope.MODELS_READ,
    )
    with pytest.raises(InsufficientScopeError):
        _ = await authenticator.authenticate(_authorization(token), DownstreamScope.CHAT_WRITE)

    # Then: the valid scope committed one use before return and missing scope committed none.
    async with migrated_session_factory() as session:
        row = await session.get(DownstreamTokenRow, principal.token_id)
    assert row is not None
    assert principal.scopes == (DownstreamScope.MODELS_READ,)
    assert row.request_count == 1
    assert row.last_used_at == fixed_clock.now()


async def test_wrong_revoked_and_admin_bearers_never_authenticate_downstream(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    fixed_clock: Clock,
) -> None:
    # Given: one downstream token that is revoked after issuance.
    repository, token = await _issued_models_token(migrated_session_factory, fixed_clock)
    authenticator = DownstreamAuthenticator(repository)
    digest = sha256(token.encode()).digest()
    async with migrated_session_factory() as session:
        row = await session.scalar(
            select(DownstreamTokenRow).where(DownstreamTokenRow.token_digest == digest)
        )
        assert row is not None
        token_id = row.id
    await repository.revoke(token_id, request_id="revoke-models")

    # When: revoked, wrong same-shape, and admin bearers authenticate downstream.
    candidates = (
        token,
        f"nblb_ds_{'f' * 64}",
        f"nblb_admin_{'f' * 64}",
    )
    failures: list[AuthRealm] = []
    for candidate in candidates:
        with pytest.raises(AuthenticationRejectedError) as captured:
            _ = await authenticator.authenticate(
                _authorization(candidate),
                DownstreamScope.MODELS_READ,
            )
        failures.append(captured.value.realm)

    # Then: every case is the same downstream 401-domain outcome with no counter increment.
    async with migrated_session_factory() as session:
        persisted = await session.get(DownstreamTokenRow, token_id)
    assert failures == [AuthRealm.DOWNSTREAM] * len(candidates)
    assert persisted is not None
    assert persisted.request_count == 0
