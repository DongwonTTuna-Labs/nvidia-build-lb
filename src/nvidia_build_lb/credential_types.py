"""Shared typed identities, clocks, and expected credential outcomes."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from typing import Protocol, override
from uuid import UUID

from nvidia_build_lb.admin.schemas import DownstreamScope


@unique
class AuthRealm(StrEnum):
    """Closed and non-interchangeable bearer realms."""

    ADMIN = "nvidia-build-lb-admin"
    DOWNSTREAM = "nvidia-build-lb"


@dataclass(slots=True)
class AuthenticationRejectedError(Exception):
    """Represent missing, malformed, wrong, or revoked realm credentials."""

    realm: AuthRealm

    @override
    def __str__(self) -> str:
        return "authentication_rejected"


@dataclass(slots=True)
class InsufficientScopeError(Exception):
    """Represent a valid downstream bearer missing one exact scope."""

    required_scope: DownstreamScope

    @override
    def __str__(self) -> str:
        return "insufficient_scope"


@dataclass(frozen=True, slots=True)
class DownstreamPrincipal:
    """Authenticated downstream identity after its use counter commits."""

    token_id: UUID
    scopes: tuple[DownstreamScope, ...]


class Clock(Protocol):
    """Supply aware timestamps at transaction boundaries."""

    def now(self) -> datetime:
        """Return the current aware UTC timestamp."""
        ...


@dataclass(slots=True)
class ResourceConflictError(Exception):
    """Represent an expected duplicate or invalid state transition."""

    resource: str

    @override
    def __str__(self) -> str:
        return "resource_conflict"


@dataclass(slots=True)
class ResourceNotFoundError(Exception):
    """Represent an expected missing credential identity."""

    resource: str
    resource_id: UUID

    @override
    def __str__(self) -> str:
        return "resource_not_found"


@dataclass(slots=True)
class InvalidAdminRequestError(Exception):
    """Reject unsupported administration query input without retaining it."""

    @override
    def __str__(self) -> str:
        return "invalid_request"
