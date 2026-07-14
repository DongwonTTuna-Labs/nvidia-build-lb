"""Digest-only downstream token repository seam."""

import hmac
from dataclasses import dataclass
from hashlib import sha256
from secrets import token_hex
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    DownstreamScope,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    DownstreamTokenRead,
    EventOutcome,
    EventType,
)
from nvidia_build_lb.credential_types import (
    AuthenticationRejectedError,
    AuthRealm,
    Clock,
    DownstreamPrincipal,
    InsufficientScopeError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from nvidia_build_lb.db_models import AdminEventRow, DownstreamTokenRow


class TokenHexSource(Protocol):
    """Generate one 256-bit lowercase hexadecimal token suffix."""

    def generate(self) -> str:
        """Return exactly 64 lowercase hexadecimal characters."""
        ...


@dataclass(frozen=True, slots=True)
class SystemTokenHexSource:
    """Use the operating-system CSPRNG for production token suffixes."""

    def generate(self) -> str:
        """Return 256 random bits encoded as lowercase hexadecimal."""
        return token_hex(32)


@dataclass(frozen=True, slots=True)
class DownstreamTokenDependencies:
    """Concrete persistence, time, and entropy dependencies."""

    sessions: async_sessionmaker[AsyncSession]
    clock: Clock
    token_hex: TokenHexSource


@dataclass(frozen=True, slots=True)
class DownstreamTokenRepository:
    """Own one-time issuance, listing, authentication, and revocation."""

    dependencies: DownstreamTokenDependencies

    async def issue(
        self,
        payload: DownstreamTokenIssueRequest,
        request_id: str,
    ) -> DownstreamTokenIssued:
        """Persist one digest and return its bearer only from this call."""
        token = f"nblb_ds_{self.dependencies.token_hex.generate()}"
        now = self.dependencies.clock.now()
        token_id = uuid4()
        row = DownstreamTokenRow(
            id=token_id,
            label=payload.label,
            label_bytes=payload.label.encode(),
            token_digest=sha256(token.encode()).digest(),
            models_read=DownstreamScope.MODELS_READ in payload.scopes,
            chat_write=DownstreamScope.CHAT_WRITE in payload.scopes,
            revoked_at=None,
            request_count=0,
            last_used_at=None,
            created_at=now,
        )
        try:
            async with self.dependencies.sessions.begin() as session:
                session.add(row)
                session.add(
                    AdminEventRow(
                        id=uuid4(),
                        request_id=request_id,
                        event_type=EventType.DOWNSTREAM_ISSUED.value,
                        upstream_key_id=None,
                        downstream_token_id=token_id,
                        outcome_class=EventOutcome.SUCCEEDED.value,
                        status_class=None,
                        latency_ms=None,
                        occurred_at=now,
                    )
                )
        except IntegrityError:
            raise ResourceConflictError(resource="downstream_token") from None
        return DownstreamTokenIssued(
            id=token_id,
            label=row.label,
            scopes=_scopes(row),
            token=token,
            revoked_at=None,
            request_count=0,
            last_used_at=None,
            created_at=now,
        )

    async def list_all(self) -> DownstreamTokenListResponse:
        """Return active and revoked rows without bearer or digest material."""
        async with self.dependencies.sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(DownstreamTokenRow).order_by(
                            DownstreamTokenRow.created_at.asc(),
                            DownstreamTokenRow.id.asc(),
                        )
                    )
                ).all()
            )
        return DownstreamTokenListResponse(items=tuple(_to_read(row) for row in rows))

    async def find_exact_label(self, label: str) -> DownstreamTokenRead | None:
        """Reconcile one issuance identity without recovering its bearer."""
        async with self.dependencies.sessions() as session:
            row = await session.scalar(
                select(DownstreamTokenRow).where(DownstreamTokenRow.label_bytes == label.encode())
            )
        return None if row is None else _to_read(row)

    async def revoke(self, token_id: UUID, request_id: str) -> None:
        """Revoke one active token and reject unknown or repeated revocation."""
        async with self.dependencies.sessions.begin() as session:
            row = await session.get(DownstreamTokenRow, token_id, with_for_update=True)
            if row is None or row.revoked_at is not None:
                raise ResourceNotFoundError(resource="downstream_token", resource_id=token_id)
            row.revoked_at = self.dependencies.clock.now()
            session.add(
                AdminEventRow(
                    id=uuid4(),
                    request_id=request_id,
                    event_type=EventType.DOWNSTREAM_REVOKED.value,
                    upstream_key_id=None,
                    downstream_token_id=token_id,
                    outcome_class=EventOutcome.SUCCEEDED.value,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=row.revoked_at,
                )
            )

    async def authorize_digest(
        self,
        digest: bytes,
        required_scope: DownstreamScope,
    ) -> DownstreamPrincipal:
        """Commit one use only after active-token and exact-scope checks."""
        async with self.dependencies.sessions.begin() as session:
            row = await session.scalar(
                select(DownstreamTokenRow)
                .where(DownstreamTokenRow.token_digest == digest)
                .with_for_update()
            )
            comparison = hmac.compare_digest(
                b"\x00" * 32 if row is None else row.token_digest,
                digest,
            )
            if row is None or not comparison or row.revoked_at is not None:
                raise AuthenticationRejectedError(realm=AuthRealm.DOWNSTREAM)
            scopes = _scopes(row)
            if required_scope not in scopes:
                raise InsufficientScopeError(required_scope=required_scope)
            row.request_count += 1
            row.last_used_at = self.dependencies.clock.now()
            return DownstreamPrincipal(token_id=row.id, scopes=scopes)


def _scopes(row: DownstreamTokenRow) -> tuple[DownstreamScope, ...]:
    return tuple(
        scope
        for scope, granted in (
            (DownstreamScope.MODELS_READ, row.models_read),
            (DownstreamScope.CHAT_WRITE, row.chat_write),
        )
        if granted
    )


def _to_read(row: DownstreamTokenRow) -> DownstreamTokenRead:
    return DownstreamTokenRead(
        id=row.id,
        label=row.label,
        scopes=_scopes(row),
        revoked_at=row.revoked_at,
        request_count=row.request_count,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )
