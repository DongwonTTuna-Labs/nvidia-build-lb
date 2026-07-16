"""Injected dependencies for the composed public and administration API."""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

import anyio
from sqlalchemy.exc import SQLAlchemyError

from nvidia_build_lb.active_routed_requests import ActiveRoutedRequestRegistry
from nvidia_build_lb.admin_credentials import CredentialServices
from nvidia_build_lb.credential_protocols import CredentialRepositorySurface
from nvidia_build_lb.logging import ServiceLogger
from nvidia_build_lb.routing import RoutedResult, RoutedStream
from nvidia_build_lb.streaming import ChatStreamResponder


class ReadinessProbe(Protocol):
    """Report current database-backed request readiness."""

    async def is_ready(self) -> bool:
        """Return true only when new requests may be accepted."""
        ...


@dataclass(frozen=True, slots=True)
class RepositoryReadinessProbe:
    """Project health readiness from the same repository overview as admin."""

    repositories: CredentialRepositorySurface
    deadline_seconds: float = 5

    async def is_ready(self) -> bool:
        """Return the current repository-backed overview readiness."""
        try:
            with anyio.fail_after(self.deadline_seconds):
                return (await self.repositories.overview()).ready
        except (OSError, SQLAlchemyError, TimeoutError):
            return False


class PublicChatRouter(Protocol):
    """Route one validated public chat request."""

    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedResult:
        """Return one fully classified routed result."""
        ...

    async def cancel_unhanded_stream(self, routed: RoutedStream) -> None:
        """Cancel and retire a live stream that no responder accepted."""
        ...


class ChatResponderFactory(Protocol):
    """Create request-scoped terminal arbitration state."""

    def create(self, stream_log: "StreamLogContext | None" = None) -> ChatStreamResponder:
        """Return a fresh responder for one routed result."""
        ...


@dataclass(frozen=True, slots=True)
class StreamLogContext:
    """Safe routed identity needed to log a durable live-stream terminal."""

    key_id: UUID
    attempt_ordinal: int

    def __post_init__(self) -> None:
        """Reject a context that cannot identify a real routed attempt."""
        if self.attempt_ordinal < 1:
            raise ValueError


@dataclass(frozen=True, slots=True)
class ApplicationServices:
    """Complete injected service graph used by HTTP composition."""

    credentials: CredentialServices
    routing: PublicChatRouter
    responders: ChatResponderFactory
    readiness: ReadinessProbe
    logger: ServiceLogger
    public_port: int = 2456
    active_requests: ActiveRoutedRequestRegistry = field(
        default_factory=ActiveRoutedRequestRegistry
    )
