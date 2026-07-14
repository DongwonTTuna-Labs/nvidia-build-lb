"""Strict administration request and response DTOs."""

from typing import Annotated, ClassVar, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from nvidia_build_lb.admin._types import (
    CanonicalScopes,
    DownstreamLabel,
    DownstreamScope,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    NonNegativeCounter,
    OneTimeToken,
    OpaqueIdentifier,
    OverviewStatus,
    ProbeStatus,
    UpstreamCredential,
    UtcTimestamp,
    WireFingerprint,
)

__all__: tuple[str, ...] = (
    "AdminEventListResponse",
    "AdminEventRead",
    "AdminOverviewRead",
    "AdminValidationErrorResponse",
    "DownstreamScope",
    "DownstreamTokenIssueRequest",
    "DownstreamTokenIssued",
    "DownstreamTokenListResponse",
    "DownstreamTokenRead",
    "EventOutcome",
    "EventType",
    "HealthState",
    "LastStatusClass",
    "OverviewStatus",
    "ProbeStatus",
    "UpstreamKeyCreateRequest",
    "UpstreamKeyListResponse",
    "UpstreamKeyRead",
    "UpstreamProbeResponse",
)


class _AdminDTO(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)


class UpstreamKeyCreateRequest(_AdminDTO):
    """Opaque upstream credential creation input."""

    key: UpstreamCredential


class UpstreamKeyRead(_AdminDTO):
    """Secret-free upstream key state."""

    id: UUID
    fingerprint: WireFingerprint
    enabled: bool
    health_state: HealthState
    cooldown_until: UtcTimestamp | None
    request_count: NonNegativeCounter
    success_count: NonNegativeCounter
    failure_count: NonNegativeCounter
    last_status_class: LastStatusClass | None
    last_used_at: UtcTimestamp | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class UpstreamKeyListResponse(_AdminDTO):
    """Complete ordered upstream key collection."""

    items: tuple[UpstreamKeyRead, ...]


class UpstreamProbeResponse(_AdminDTO):
    """One key's explicit probe outcome."""

    id: UUID
    enabled: bool
    probe_status: ProbeStatus
    observed_at: UtcTimestamp


class DownstreamTokenIssueRequest(_AdminDTO):
    """Unique label and exact scopes for one token issuance."""

    label: DownstreamLabel
    scopes: CanonicalScopes


class _DownstreamTokenState(_AdminDTO):
    label: DownstreamLabel
    scopes: CanonicalScopes
    id: UUID
    revoked_at: UtcTimestamp | None
    request_count: NonNegativeCounter
    last_used_at: UtcTimestamp | None
    created_at: UtcTimestamp


class DownstreamTokenIssued(_DownstreamTokenState):
    """One-time plaintext downstream token response."""

    token: OneTimeToken


class DownstreamTokenRead(_DownstreamTokenState):
    """Digest-free persisted downstream token state."""


class DownstreamTokenListResponse(_AdminDTO):
    """Complete ordered downstream token collection."""

    items: tuple[DownstreamTokenRead, ...]


class UpstreamKeyOverview(_AdminDTO):
    """Closed current upstream aggregate counts."""

    total: NonNegativeCounter
    enabled: NonNegativeCounter
    eligible: NonNegativeCounter
    cooling: NonNegativeCounter
    degraded: NonNegativeCounter


class DownstreamTokenOverview(_AdminDTO):
    """Closed current downstream aggregate counts."""

    total: NonNegativeCounter
    active: NonNegativeCounter
    revoked: NonNegativeCounter


class AdminOverviewRead(_AdminDTO):
    """Operational readiness and aggregate counters."""

    status: OverviewStatus
    ready: bool
    upstream_keys: UpstreamKeyOverview
    downstream_tokens: DownstreamTokenOverview
    request_count: NonNegativeCounter
    last_event_at: UtcTimestamp | None
    generated_at: UtcTimestamp


class AdminEventRead(_AdminDTO):
    """One secret-free durable event."""

    id: UUID
    request_id: OpaqueIdentifier
    event_type: EventType
    upstream_key_id: UUID | None
    downstream_token_id: UUID | None
    outcome_class: EventOutcome
    status_class: LastStatusClass | None
    latency_ms: NonNegativeCounter | None
    occurred_at: UtcTimestamp


class AdminEventListResponse(_AdminDTO):
    """Newest one hundred secret-free events."""

    items: Annotated[tuple[AdminEventRead, ...], Field(max_length=100)]


class _AdminValidationErrorDetail(_AdminDTO):
    code: Literal["invalid_request"] = "invalid_request"
    message: Literal["request validation failed"] = "request validation failed"
    request_id: OpaqueIdentifier


class AdminValidationErrorResponse(_AdminDTO):
    """Fixed safe replacement for framework validation detail."""

    error: _AdminValidationErrorDetail

    @classmethod
    def from_request_id(cls, request_id: str) -> Self:
        """Construct the sole safe admin 422 response."""
        return cls(error=_AdminValidationErrorDetail(request_id=request_id))
