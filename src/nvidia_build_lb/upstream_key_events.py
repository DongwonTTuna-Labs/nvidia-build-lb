"""Canonical safe admin events for upstream-key lifecycle mutations."""

from datetime import datetime
from uuid import UUID, uuid4

from nvidia_build_lb.admin.schemas import EventOutcome, EventType
from nvidia_build_lb.db_models import EVENT_WRITER_GENERATION, AdminEventRow


def upstream_key_event(
    *,
    request_id: str,
    event_type: EventType,
    key_id: UUID,
    fingerprint: str,
    occurred_at: datetime,
) -> AdminEventRow:
    """Build one generation-marked upstream-key configuration event."""
    return AdminEventRow(
        id=uuid4(),
        request_id=request_id,
        event_type=event_type.value,
        upstream_key_id=key_id,
        upstream_key_fingerprint=fingerprint,
        downstream_token_id=None,
        outcome_class=EventOutcome.SUCCEEDED.value,
        status_class=None,
        latency_ms=None,
        occurred_at=occurred_at,
        writer_generation=EVENT_WRITER_GENERATION,
    )
