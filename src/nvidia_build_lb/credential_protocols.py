"""Narrow repository protocols used by HTTP composition and test fakes."""

from typing import Protocol
from uuid import UUID

from nvidia_build_lb.admin.schemas import (
    AdminEventListResponse,
    AdminOverviewRead,
    DownstreamScope,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
)
from nvidia_build_lb.credential_types import DownstreamPrincipal


class UpstreamCredentialRepository(Protocol):
    """Administration operations over encrypted upstream keys."""

    async def list_all(self) -> UpstreamKeyListResponse:
        """Return every secret-free upstream key projection."""
        ...

    async def create(self, payload: UpstreamKeyCreateRequest, request_id: str) -> UpstreamKeyRead:
        """Encrypt and persist one disabled upstream key."""
        ...

    async def enable(self, key_id: UUID, request_id: str) -> None:
        """Enable one existing upstream key idempotently."""
        ...

    async def disable(self, key_id: UUID, request_id: str) -> None:
        """Disable one existing upstream key idempotently."""
        ...

    async def delete(self, key_id: UUID, request_id: str) -> None:
        """Delete one disabled upstream key."""
        ...


class DownstreamCredentialRepository(Protocol):
    """Administration and authorization operations over downstream tokens."""

    async def list_all(self) -> DownstreamTokenListResponse:
        """Return every digest-free downstream token projection."""
        ...

    async def issue(
        self,
        payload: DownstreamTokenIssueRequest,
        request_id: str,
    ) -> DownstreamTokenIssued:
        """Issue one scoped downstream bearer for one-time display."""
        ...

    async def revoke(self, token_id: UUID, request_id: str) -> None:
        """Revoke one downstream token idempotently."""
        ...

    async def authorize_digest(
        self,
        digest: bytes,
        required_scope: DownstreamScope,
    ) -> DownstreamPrincipal:
        """Authorize a token digest and commit its counted use."""
        ...


class CredentialRepositorySurface(Protocol):
    """Complete repository surface consumed by admin route composition."""

    @property
    def upstream(self) -> UpstreamCredentialRepository:
        """Return the upstream administration repository."""
        ...

    @property
    def downstream(self) -> DownstreamCredentialRepository:
        """Return the downstream administration and auth repository."""
        ...

    async def overview(self) -> AdminOverviewRead:
        """Return the secret-free administration aggregate."""
        ...

    async def events(self) -> AdminEventListResponse:
        """Return the bounded safe event collection."""
        ...
