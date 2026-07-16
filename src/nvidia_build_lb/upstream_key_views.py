"""Safe upstream credential read-model projections."""

from datetime import datetime

from nvidia_build_lb.admin.schemas import (
    HealthState,
    LastStatusClass,
    UpstreamKeyRead,
    UpstreamRoutingState,
)
from nvidia_build_lb.db_models import UpstreamKeyRow


def routing_state_for(row: UpstreamKeyRow, now: datetime) -> UpstreamRoutingState:
    """Project scheduler eligibility without exposing credential material."""
    if row.quarantined:
        return UpstreamRoutingState.QUARANTINED
    if row.cooldown_until is not None and row.cooldown_until > now:
        return UpstreamRoutingState.COOLDOWN
    if not row.enabled:
        return UpstreamRoutingState.DISABLED
    return UpstreamRoutingState.ELIGIBLE


def upstream_key_read(row: UpstreamKeyRow, now: datetime) -> UpstreamKeyRead:
    """Convert one persistence row into the closed safe administration DTO."""
    last_status = None if row.last_status_class is None else LastStatusClass(row.last_status_class)
    return UpstreamKeyRead(
        id=row.id,
        fingerprint=f"sha256:{row.fingerprint}",
        enabled=row.enabled,
        routing_state=routing_state_for(row, now),
        health_state=HealthState(row.health_state),
        cooldown_until=row.cooldown_until,
        request_count=row.request_count,
        success_count=row.success_count,
        failure_count=row.failure_count,
        last_status_class=last_status,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
