"""Public durable scheduler commands, leases, and safe errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import TYPE_CHECKING, override

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from nvidia_build_lb.admin.schemas import LastStatusClass
    from nvidia_build_lb.admin_ledger import AdminLedger
    from nvidia_build_lb.credential_types import Clock
    from nvidia_build_lb.db_models import SchedulerStateRow, UpstreamKeyRow


@unique
class TerminalOutcome(StrEnum):
    """Closed terminal outcomes for one logical attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AttemptLease:
    """Proof that pre-network state committed for one exact key."""

    key_id: UUID
    request_id: str
    started_event_id: UUID
    started_at: datetime
    explicit_probe: bool


@dataclass(frozen=True, slots=True)
class AttemptTerminal:
    """Terminal observation applied under row lock."""

    outcome: TerminalOutcome
    status_class: LastStatusClass
    latency_ms: int
    cooldown_until: datetime | None


@dataclass(frozen=True, slots=True)
class SchedulerDependencies:
    """Concrete persistence and time dependencies."""

    sessions: async_sessionmaker[AsyncSession]
    clock: Clock
    ledger: AdminLedger | None = None


@dataclass(frozen=True, slots=True)
class LockedAttempt:
    """Rows and session held by one scheduler transaction."""

    session: AsyncSession
    scheduler: SchedulerStateRow
    key: UpstreamKeyRow


@dataclass(slots=True)
class SchedulerStateUnavailableError(Exception):
    """Reject a database missing its seeded singleton row."""

    @override
    def __str__(self) -> str:
        return "scheduler_state_unavailable"


@unique
class NoEligibleReason(StrEnum):
    """Closed safe classifications when no row is immediately selectable."""

    NO_KEYS = "no_keys"
    RATE_COOLDOWN = "rate_cooldown"
    TRANSIENT_COOLDOWN = "transient_cooldown"


@dataclass(slots=True)
class NoEligibleUpstreamKeyError(Exception):
    """Reject selection when no row is currently routable."""

    reason: NoEligibleReason = NoEligibleReason.NO_KEYS
    retry_after_seconds: int | None = None

    @override
    def __str__(self) -> str:
        return "no_eligible_upstream_key"
