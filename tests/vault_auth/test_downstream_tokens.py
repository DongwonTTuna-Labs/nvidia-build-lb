from dataclasses import dataclass
from hashlib import sha256
from typing import override

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import DownstreamScope, DownstreamTokenIssueRequest
from nvidia_build_lb.credential_types import Clock, ResourceConflictError, ResourceNotFoundError
from nvidia_build_lb.db_models import DownstreamTokenRow
from nvidia_build_lb.downstream_tokens import (
    DownstreamTokenDependencies,
    DownstreamTokenRepository,
    TokenHexSource,
)

pytestmark = [pytest.mark.vault_auth, pytest.mark.anyio]


@dataclass(frozen=True, slots=True)
class FixedTokenHexSource(TokenHexSource):
    suffixes: tuple[str, ...]
    _index: list[int]

    @override
    def generate(self) -> str:
        index = self._index[0]
        self._index[0] = index + 1
        return self.suffixes[index]


def _repository(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
    *suffixes: str,
) -> DownstreamTokenRepository:
    source = FixedTokenHexSource(suffixes=suffixes, _index=[0])
    return DownstreamTokenRepository(DownstreamTokenDependencies(sessions, clock, source))


async def test_downstream_issue_returns_plaintext_once_and_persists_only_digest(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    fixed_clock: Clock,
) -> None:
    # Given: one exact label, reversed scopes, and deterministic synthetic entropy.
    suffix = "a" * 64
    repository = _repository(migrated_session_factory, fixed_clock, suffix)
    request = DownstreamTokenIssueRequest(
        label="hermes-cutover:00000000-0000-4000-8000-000000000001",
        scopes=(DownstreamScope.CHAT_WRITE, DownstreamScope.MODELS_READ),
    )

    # When: the token is issued and the durable list is read later.
    issued = await repository.issue(request, request_id="issue-request")
    listed = await repository.list_all()

    # Then: plaintext is one-time only, scopes are canonical, and storage is digest-only.
    expected_token = f"nblb_ds_{suffix}"
    assert issued.token == expected_token
    assert issued.scopes == (DownstreamScope.MODELS_READ, DownstreamScope.CHAT_WRITE)
    assert len(listed.items) == 1
    assert "token" not in listed.items[0].model_dump()
    assert expected_token not in listed.model_dump_json()
    async with migrated_session_factory() as session:
        row = await session.get(DownstreamTokenRow, issued.id)
        assert row is not None
        stored_digest = row.token_digest
    assert stored_digest == sha256(expected_token.encode()).digest()
    assert expected_token.encode() != stored_digest


async def test_downstream_label_remains_unique_after_revoke_without_blind_retry(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    fixed_clock: Clock,
) -> None:
    # Given: one issued and revoked label plus unused synthetic entropy.
    repository = _repository(
        migrated_session_factory,
        fixed_clock,
        "b" * 64,
        "c" * 64,
    )
    request = DownstreamTokenIssueRequest(
        label="hermes-cutover:00000000-0000-4000-8000-000000000002",
        scopes=(DownstreamScope.CHAT_WRITE,),
    )
    issued = await repository.issue(request, request_id="first")
    await repository.revoke(issued.id, request_id="revoke")

    # When: the exact binary label is issued again.
    with pytest.raises(ResourceConflictError):
        _ = await repository.issue(request, request_id="blind-retry")

    # Then: one revoked identity remains and reconciliation is secret-free.
    reconciled = await repository.find_exact_label(request.label)
    async with migrated_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(DownstreamTokenRow))
    assert reconciled is not None
    assert reconciled.id == issued.id
    assert reconciled.revoked_at == fixed_clock.now()
    assert count == 1


async def test_downstream_revoke_is_not_repeatable_and_list_order_is_stable(
    migrated_session_factory: async_sessionmaker[AsyncSession],
    fixed_clock: Clock,
) -> None:
    # Given: two active tokens created at the same timestamp.
    repository = _repository(migrated_session_factory, fixed_clock, "d" * 64, "e" * 64)
    first = await repository.issue(
        DownstreamTokenIssueRequest(label="token-a", scopes=(DownstreamScope.MODELS_READ,)),
        request_id="first",
    )
    _ = await repository.issue(
        DownstreamTokenIssueRequest(label="token-b", scopes=(DownstreamScope.CHAT_WRITE,)),
        request_id="second",
    )

    # When: one token is revoked and the complete list is read.
    await repository.revoke(first.id, request_id="revoke")
    listed = await repository.list_all()

    # Then: repeated/unknown revokes are 404-domain outcomes and ordering is stable.
    with pytest.raises(ResourceNotFoundError):
        await repository.revoke(first.id, request_id="repeat")
    order = tuple((item.created_at, item.id.int) for item in listed.items)
    assert order == tuple(sorted(order))
    assert listed.items[0].revoked_at is not None or listed.items[1].revoked_at is not None
