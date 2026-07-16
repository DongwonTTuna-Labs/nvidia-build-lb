"""Closed administration wire values and scalar parsers."""

from datetime import UTC, datetime, timedelta
from enum import StrEnum, unique
from typing import Annotated, Final
from unicodedata import category

from pydantic import AfterValidator, Field
from pydantic_core import PydanticCustomError

_SURROGATE_MIN: Final = 0xD800
_SURROGATE_MAX: Final = 0xDFFF
_MAX_UPSTREAM_KEY_BYTES: Final = 4096
_MAX_LABEL_SCALARS: Final = 128
_UPSTREAM_KEY_ERROR: Final = "upstream_key"
_INVALID_UTF8_MESSAGE: Final = "credential must contain valid UTF-8"
_INVALID_UPSTREAM_KEY_MESSAGE: Final = "credential does not satisfy the opaque boundary"
_DOWNSTREAM_LABEL_ERROR: Final = "downstream_label"
_INVALID_LABEL_MESSAGE: Final = "label does not satisfy the boundary"
_DOWNSTREAM_SCOPES_ERROR: Final = "downstream_scopes"
_DUPLICATE_SCOPES_MESSAGE: Final = "scopes must be unique"
_UTC_TIMESTAMP_ERROR: Final = "utc_timestamp"
_INVALID_TIMESTAMP_MESSAGE: Final = "timestamp must be UTC"


@unique
class HealthState(StrEnum):
    """Route-meaningful upstream health states."""

    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"


@unique
class UpstreamRoutingState(StrEnum):
    """Operator-safe reason a persisted key is or is not selectable."""

    DISABLED = "disabled"
    ELIGIBLE = "eligible"
    COOLDOWN = "cooldown"
    QUARANTINED = "quarantined"


@unique
class LastStatusClass(StrEnum):
    """Safe terminal classifications shared by keys and events."""

    SUCCESS = "success"
    INVALID_CREDENTIAL = "invalid_credential"
    CREDITS_EXHAUSTED = "credits_exhausted"
    RATE_LIMITED = "rate_limited"
    REQUEST_REJECTED = "request_rejected"
    TIMEOUT = "timeout"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    UPSTREAM_BAD_GATEWAY = "upstream_bad_gateway"
    UPSTREAM_INTERNAL_ERROR = "upstream_internal_error"
    UPSTREAM_PROTOCOL_ERROR = "upstream_protocol_error"
    CANCELLED = "cancelled"
    DELIVERY_FAILED = "delivery_failed"


@unique
class ProbeStatus(StrEnum):
    """Closed probe outcomes that never imply enabling a key."""

    VALID = "valid"
    INVALID_CREDENTIAL = "invalid_credential"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"


@unique
class DownstreamScope(StrEnum):
    """The only downstream API permissions."""

    MODELS_READ = "models:read"
    CHAT_WRITE = "chat:write"


@unique
class OverviewStatus(StrEnum):
    """Overall service status shared with health readiness."""

    OK = "ok"
    DEGRADED = "degraded"


@unique
class RuntimeState(StrEnum):
    """Process-lifecycle state sampled around one dashboard snapshot."""

    OPERATIONAL = "operational"
    UNAVAILABLE = "unavailable"


@unique
class ReadinessCause(StrEnum):
    """Single highest-priority reason for the dashboard readiness value."""

    READY = "ready"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    LEDGER_CAPACITY_EXHAUSTED = "ledger_capacity_exhausted"
    NO_ELIGIBLE_UPSTREAM = "no_eligible_upstream"


@unique
class LedgerStatus(StrEnum):
    """Closed capacity and maintenance states for the administration ledger."""

    OK = "ok"
    MAINTENANCE_OVERDUE = "maintenance_overdue"
    CAPACITY_EXHAUSTED_RECOVERING = "capacity_exhausted_recovering"
    CAPACITY_BLOCKED = "capacity_blocked"


@unique
class CapacityBlocker(StrEnum):
    """Exact reason a successful maintenance assessment could not recover capacity."""

    NONE = "none"
    ACTIVE_ATTEMPTS = "active_attempts"
    RECONCILIATION_GRACE = "reconciliation_grace"
    LOCK_CONTENTION = "lock_contention"
    ORPHANED_PENDING = "orphaned_pending"
    LEGACY_UNLINKED = "legacy_unlinked"


@unique
class EventType(StrEnum):
    """Safe administration event categories."""

    UPSTREAM_KEY_CREATED = "upstream_key_created"
    UPSTREAM_KEY_ENABLED = "upstream_key_enabled"
    UPSTREAM_KEY_DISABLED = "upstream_key_disabled"
    UPSTREAM_KEY_DELETED = "upstream_key_deleted"
    UPSTREAM_PROBE = "upstream_probe"
    DOWNSTREAM_ISSUED = "downstream_token_issued"
    DOWNSTREAM_REVOKED = "downstream_token_revoked"
    UPSTREAM_ATTEMPT = "upstream_attempt"


@unique
class EventOutcome(StrEnum):
    """Lifecycle outcomes for persisted safe events."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _parse_upstream_credential(value: str) -> str:
    if any(_SURROGATE_MIN <= ord(character) <= _SURROGATE_MAX for character in value):
        raise PydanticCustomError(_UPSTREAM_KEY_ERROR, _INVALID_UTF8_MESSAGE)
    byte_length = len(value.encode())
    has_forbidden_character = any(character in "\x00\r\n" for character in value)
    if byte_length < 1 or byte_length > _MAX_UPSTREAM_KEY_BYTES or has_forbidden_character:
        raise PydanticCustomError(_UPSTREAM_KEY_ERROR, _INVALID_UPSTREAM_KEY_MESSAGE)
    return value


def _parse_label(value: str) -> str:
    has_disallowed_scalar = any(
        _SURROGATE_MIN <= ord(character) <= _SURROGATE_MAX or category(character) == "Cc"
        for character in value
    )
    if (
        not value
        or len(value) > _MAX_LABEL_SCALARS
        or value != value.strip()
        or has_disallowed_scalar
    ):
        raise PydanticCustomError(_DOWNSTREAM_LABEL_ERROR, _INVALID_LABEL_MESSAGE)
    return value


def _canonicalize_scopes(scopes: tuple[DownstreamScope, ...]) -> tuple[DownstreamScope, ...]:
    if len(scopes) != len(set(scopes)):
        raise PydanticCustomError(_DOWNSTREAM_SCOPES_ERROR, _DUPLICATE_SCOPES_MESSAGE)
    return tuple(
        scope
        for scope in (DownstreamScope.MODELS_READ, DownstreamScope.CHAT_WRITE)
        if scope in scopes
    )


def _parse_utc_timestamp(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise PydanticCustomError(_UTC_TIMESTAMP_ERROR, _INVALID_TIMESTAMP_MESSAGE)
    return value.astimezone(UTC)


type UpstreamCredential = Annotated[
    str,
    Field(strict=True),
    AfterValidator(_parse_upstream_credential),
]
type DownstreamLabel = Annotated[str, Field(strict=True), AfterValidator(_parse_label)]
type ParsedDownstreamScope = Annotated[DownstreamScope, Field(strict=False)]
type CanonicalScopes = Annotated[
    tuple[ParsedDownstreamScope, ...],
    Field(min_length=1, max_length=2, strict=False),
    AfterValidator(_canonicalize_scopes),
]
type NonNegativeCounter = Annotated[int, Field(ge=0, le=9_223_372_036_854_775_807, strict=True)]
type OpaqueIdentifier = Annotated[str, Field(min_length=1, strict=True)]
type UtcTimestamp = Annotated[datetime, AfterValidator(_parse_utc_timestamp)]
type WireFingerprint = Annotated[
    str,
    Field(min_length=71, max_length=71, pattern=r"sha256:[0-9a-f]{64}", strict=True),
]
type OneTimeToken = Annotated[
    str,
    Field(min_length=72, max_length=72, pattern=r"nblb_ds_[0-9a-f]{64}", strict=True),
]
