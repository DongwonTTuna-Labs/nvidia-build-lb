"""Dependency bundle for durable attempt repository transactions."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin_ledger import AdminLedger, default_admin_ledger
from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.vault import Vault


@dataclass(frozen=True, slots=True)
class AttemptRepositoryDependencies:
    """Persistence, vault, time, fail-stop, and ledger dependencies."""

    sessions: async_sessionmaker[AsyncSession]
    vault: Vault
    clock: Clock
    fail_stop: AttemptFailStop
    ledger: AdminLedger | None = None

    def resolved_ledger(self) -> AdminLedger:
        """Return the shared production ledger or an isolated-test default."""
        return (
            self.ledger
            if self.ledger is not None
            else default_admin_ledger(self.sessions, self.clock)
        )
