"""Strict administration request and response DTOs."""

from typing import Annotated
from uuid import UUID

from pydantic import Field

from nvidia_build_lb.admin._types import (
    CanonicalScopes,
    CapacityBlocker,
    DownstreamLabel,
    DownstreamScope,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    LedgerStatus,
    NonNegativeCounter,
    OneTimeToken,
    OpaqueIdentifier,
    OverviewStatus,
    ProbeStatus,
    ReadinessCause,
    RuntimeState,
    UpstreamCredential,
    UpstreamRoutingState,
    UtcTimestamp,
    WireFingerprint,
)
from nvidia_build_lb.admin.schema_base import AdminDTO
from nvidia_build_lb.admin.validation_schemas import AdminValidationErrorResponse

__all__: tuple[str, ...] = (
    "AdminDashboardEventListResponse",
    "AdminDashboardEventRead",
    "AdminDashboardRead",
    "AdminEventListResponse",
    "AdminEventRead",
    "AdminLedgerRead",
    "AdminOperatorReadinessRead",
    "AdminOverviewRead",
    "AdminValidationErrorResponse",
    "CapacityBlocker",
    "DownstreamScope",
    "DownstreamTokenIssueRequest",
    "DownstreamTokenIssued",
    "DownstreamTokenListResponse",
    "DownstreamTokenRead",
    "EventOutcome",
    "EventType",
    "HealthState",
    "LastStatusClass",
    "LedgerStatus",
    "OverviewStatus",
    "ProbeStatus",
    "ReadinessCause",
    "RuntimeState",
    "UpstreamKeyCreateRequest",
    "UpstreamKeyListResponse",
    "UpstreamKeyRead",
    "UpstreamProbeResponse",
    "UpstreamRoutingState",
)


class UpstreamKeyCreateRequest(AdminDTO):
    """Opaque upstream credential creation input."""

    key: UpstreamCredential


class UpstreamKeyRead(AdminDTO):
    """Secret-free upstream key state."""

    id: UUID
    fingerprint: WireFingerprint
    enabled: bool
    routing_state: UpstreamRoutingState
    health_state: HealthState
    cooldown_until: UtcTimestamp | None
    request_count: NonNegativeCounter
    success_count: NonNegativeCounter
    failure_count: NonNegativeCounter
    last_status_class: LastStatusClass | None
    last_used_at: UtcTimestamp | None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp


class UpstreamKeyListResponse(AdminDTO):
    """Complete ordered upstream key collection."""

    items: tuple[UpstreamKeyRead, ...]


class UpstreamProbeResponse(AdminDTO):
    """One key's explicit probe outcome."""

    id: UUID
    enabled: bool
    probe_status: ProbeStatus
    observed_at: UtcTimestamp


class DownstreamTokenIssueRequest(AdminDTO):
    """Unique label and exact scopes for one token issuance."""

    label: DownstreamLabel
    scopes: CanonicalScopes


class _DownstreamTokenState(AdminDTO):
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


class DownstreamTokenListResponse(AdminDTO):
    """Complete ordered downstream token collection."""

    items: tuple[DownstreamTokenRead, ...]


class UpstreamKeyOverview(AdminDTO):
    """Closed current upstream aggregate counts."""

    total: NonNegativeCounter
    enabled: NonNegativeCounter
    eligible: NonNegativeCounter
    cooling: NonNegativeCounter
    degraded: NonNegativeCounter


class DownstreamTokenOverview(AdminDTO):
    """Closed current downstream aggregate counts."""

    total: NonNegativeCounter
    active: NonNegativeCounter
    revoked: NonNegativeCounter


class AdminOverviewRead(AdminDTO):
    """Operational readiness and aggregate counters."""

    status: OverviewStatus
    ready: bool
    upstream_keys: UpstreamKeyOverview
    downstream_tokens: DownstreamTokenOverview
    request_count: NonNegativeCounter
    last_event_at: UtcTimestamp | None
    generated_at: UtcTimestamp


class AdminEventRead(AdminDTO):
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


class AdminEventListResponse(AdminDTO):
    """Newest one hundred secret-free events."""

    items: Annotated[tuple[AdminEventRead, ...], Field(max_length=100)]


class AdminDashboardEventRead(AdminEventRead):
    """Dashboard event with durable key identity and exact attempt linkage."""

    upstream_key_fingerprint: WireFingerprint | None
    attempt_started_event_id: UUID | None


class AdminDashboardEventListResponse(AdminDTO):
    """Newest one hundred events from the dashboard transaction."""

    items: Annotated[tuple[AdminDashboardEventRead, ...], Field(max_length=100)]


class AdminLedgerRead(AdminDTO):
    """Bounded retention and admission evidence from one database snapshot."""

    status: LedgerStatus
    capacity_blocker: CapacityBlocker
    event_rows: NonNegativeCounter
    reserved_terminal_slots: NonNegativeCounter
    event_capacity: NonNegativeCounter
    attempt_rows: NonNegativeCounter
    attempt_capacity: NonNegativeCounter
    last_maintenance_completed_at: UtcTimestamp | None
    last_pruned_event_rows: NonNegativeCounter
    last_pruned_attempt_rows: NonNegativeCounter
    oldest_event_at: UtcTimestamp | None


class AdminOperatorReadinessRead(AdminDTO):
    """Bounded host-operator truth without credential or event collections."""

    runtime_state: RuntimeState
    readiness_cause: ReadinessCause
    ledger_status: LedgerStatus
    capacity_blocker: CapacityBlocker


class AdminDashboardRead(AdminDTO):
    """Canonical coherent administration read consumed by the browser."""

    runtime_state: RuntimeState
    readiness_cause: ReadinessCause
    ledger: AdminLedgerRead
    overview: AdminOverviewRead
    upstream_keys: UpstreamKeyListResponse
    downstream_tokens: DownstreamTokenListResponse
    events: AdminDashboardEventListResponse
