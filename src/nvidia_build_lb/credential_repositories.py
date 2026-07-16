"""Database-backed credential repository composition."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    AdminDashboardRead,
    AdminEventListResponse,
    AdminOperatorReadinessRead,
    AdminOverviewRead,
)
from nvidia_build_lb.admin_dashboard import read_dashboard
from nvidia_build_lb.admin_ledger import AdminLedger, default_admin_ledger
from nvidia_build_lb.admin_queries import read_events, read_overview
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.downstream_tokens import DownstreamTokenRepository
from nvidia_build_lb.operator_readiness import read_operator_readiness
from nvidia_build_lb.upstream_keys import UpstreamKeyRepository


@dataclass(frozen=True, slots=True)
class CredentialRepositories:
    """Bundle database-backed credential reads and writes."""

    upstream: UpstreamKeyRepository
    downstream: DownstreamTokenRepository
    sessions: async_sessionmaker[AsyncSession]
    clock: Clock
    ledger: AdminLedger | None = None

    def resolved_ledger(self) -> AdminLedger:
        """Return the shared production ledger or an isolated-test default."""
        return (
            self.ledger
            if self.ledger is not None
            else default_admin_ledger(self.sessions, self.clock)
        )

    async def overview(self) -> AdminOverviewRead:
        """Project the exact secret-free administration aggregate."""
        return await read_overview(
            self.sessions,
            self.clock,
            self.resolved_ledger().policy,
        )

    async def dashboard(self) -> AdminDashboardRead:
        """Read the canonical coherent administration snapshot."""
        return await read_dashboard(
            self.sessions,
            self.clock,
            self.resolved_ledger().policy,
        )

    async def operator_readiness(self) -> AdminOperatorReadinessRead:
        """Read the bounded host-operator projection."""
        return await read_operator_readiness(
            self.sessions,
            self.clock,
            self.resolved_ledger().policy,
        )

    async def events(self) -> AdminEventListResponse:
        """Project the exact newest-one-hundred safe event collection."""
        return await read_events(self.sessions)
