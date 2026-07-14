"""Typed durable commands and receipts for NVIDIA routing attempts."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, unique
from uuid import UUID

from pydantic import SecretStr

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.scheduler_state import TerminalOutcome

_MAX_REQUEST_ID_LENGTH = 128
_INVALID_REQUEST_ID = "invalid_request_id"
_TOO_MANY_EXCLUDED_KEYS = "too_many_excluded_keys"
_PROBE_CANNOT_EXCLUDE_KEYS = "probe_cannot_exclude_keys"
_NEGATIVE_LATENCY = "negative_latency"
_INCOMPLETE_COOLDOWN_PAIR = "incomplete_cooldown_pair"
_INVALID_COOLDOWN_KIND_FOR_STATUS = "invalid_cooldown_kind_for_status"
_RATE_LIMIT_COOLDOWN_REQUIRED = "rate_limit_cooldown_required"


@unique
class CooldownKind(StrEnum):
    """Closed reasons for temporarily excluding an otherwise enabled key."""

    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"


_TRANSIENT_COOLDOWN_STATUSES = frozenset(
    {
        LastStatusClass.TIMEOUT,
        LastStatusClass.UPSTREAM_UNAVAILABLE,
        LastStatusClass.UPSTREAM_BAD_GATEWAY,
    }
)


@dataclass(frozen=True, slots=True)
class AttemptIdentity:
    """Stable IDs that make start and terminal operations replay-safe."""

    started_event_id: UUID
    terminal_event_id: UUID
    request_id: str
    service_epoch: UUID
    started_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptStartCommand:
    """Complete immutable input for one reservation transaction."""

    started_event_id: UUID
    terminal_event_id: UUID
    request_id: str
    service_epoch: UUID
    explicit_probe_key_id: UUID | None
    excluded_key_ids: frozenset[UUID]
    started_at: datetime

    def __post_init__(self) -> None:
        """Reject identities that cannot satisfy the durable receipt contract."""
        if not self.request_id or len(self.request_id) > _MAX_REQUEST_ID_LENGTH:
            raise ValueError(_INVALID_REQUEST_ID)
        if len(self.excluded_key_ids) > 1:
            raise ValueError(_TOO_MANY_EXCLUDED_KEYS)
        if self.explicit_probe_key_id is not None and self.excluded_key_ids:
            raise ValueError(_PROBE_CANNOT_EXCLUDE_KEYS)

    @property
    def identity(self) -> AttemptIdentity:
        """Return the subset shared with terminal commands."""
        return AttemptIdentity(
            self.started_event_id,
            self.terminal_event_id,
            self.request_id,
            self.service_epoch,
            self.started_at,
        )


@dataclass(frozen=True, slots=True)
class AttemptLease:
    """Committed selection plus the secret usable only by the adapter."""

    identity: AttemptIdentity
    key_id: UUID
    credential: SecretStr = field(repr=False)
    consecutive_rate_limits: int = 0
    consecutive_transient_failures: int = 0


@dataclass(frozen=True, slots=True)
class AttemptFinalizeCommand:
    """Complete immutable terminal transaction input."""

    identity: AttemptIdentity
    outcome: TerminalOutcome
    status_class: LastStatusClass
    latency_ms: int
    cooldown_until: datetime | None
    cooldown_kind: CooldownKind | None
    terminal_committed_at: datetime

    def __post_init__(self) -> None:
        """Reject incomplete terminal transitions before persistence."""
        if self.latency_ms < 0:
            raise ValueError(_NEGATIVE_LATENCY)
        if (self.cooldown_until is None) != (self.cooldown_kind is None):
            raise ValueError(_INCOMPLETE_COOLDOWN_PAIR)
        if self.status_class is LastStatusClass.RATE_LIMITED:
            if self.cooldown_kind is None:
                raise ValueError(_RATE_LIMIT_COOLDOWN_REQUIRED)
            if self.cooldown_kind is not CooldownKind.RATE_LIMIT:
                raise ValueError(_INVALID_COOLDOWN_KIND_FOR_STATUS)
        elif self.cooldown_kind is CooldownKind.TRANSIENT:
            if self.status_class not in _TRANSIENT_COOLDOWN_STATUSES:
                raise ValueError(_INVALID_COOLDOWN_KIND_FOR_STATUS)
        elif self.cooldown_kind is not None:
            raise ValueError(_INVALID_COOLDOWN_KIND_FOR_STATUS)

    @classmethod
    def success(
        cls,
        identity: AttemptIdentity,
        committed_at: datetime,
        *,
        latency_ms: int,
    ) -> "AttemptFinalizeCommand":
        """Create the common successful terminal command."""
        return cls(
            identity=identity,
            outcome=TerminalOutcome.SUCCEEDED,
            status_class=LastStatusClass.SUCCESS,
            latency_ms=latency_ms,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=committed_at,
        )


@dataclass(frozen=True, slots=True)
class TerminalCommitted:
    """Idempotent proof that the exact terminal payload is durable."""

    identity: AttemptIdentity


@dataclass(frozen=True, slots=True)
class StartCommitted:
    """Fresh read proves the exact start transaction and returns its lease."""

    lease: AttemptLease


@dataclass(frozen=True, slots=True)
class StartAbsent:
    """Fresh read proves that none of the stable start identifiers committed."""


@dataclass(frozen=True, slots=True)
class StartConflict:
    """Fresh read found a partial or different start payload."""


@dataclass(frozen=True, slots=True)
class StartUnknown:
    """Fresh read could not prove committed, absent, or conflicting state."""


type StartReconciliation = StartCommitted | StartAbsent | StartConflict | StartUnknown


@dataclass(frozen=True, slots=True)
class TerminalExact:
    """Fresh read proves the exact terminal transaction committed."""

    identity: AttemptIdentity


@dataclass(frozen=True, slots=True)
class TerminalPending:
    """Fresh read proves the matching start remains pending."""

    identity: AttemptIdentity


@dataclass(frozen=True, slots=True)
class TerminalAbsent:
    """Fresh read proves that the stable start identifier is absent."""


@dataclass(frozen=True, slots=True)
class TerminalConflict:
    """Fresh read found a partial or different terminal payload."""


@dataclass(frozen=True, slots=True)
class TerminalUnknown:
    """Fresh read could not prove an exact terminal state."""


type TerminalReconciliation = (
    TerminalExact | TerminalPending | TerminalAbsent | TerminalConflict | TerminalUnknown
)
