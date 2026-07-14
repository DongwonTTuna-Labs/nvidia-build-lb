"""Pure row construction and exact idempotency comparisons."""

from uuid import UUID

from nvidia_build_lb.admin.schemas import EventOutcome, EventType
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptIdentity,
    AttemptStartCommand,
)
from nvidia_build_lb.db_models import (
    AdminEventRow,
    UpstreamAttemptReceiptRow,
    UpstreamLivePinRow,
)


def start_matches(row: UpstreamAttemptReceiptRow, command: AttemptStartCommand) -> bool:
    """Compare every stable start field without inferring or normalizing."""
    return (
        row.started_event_id == command.started_event_id
        and row.terminal_event_id == command.terminal_event_id
        and row.request_id == command.request_id
        and row.service_epoch == command.service_epoch
        and row.explicit_probe_key_id == command.explicit_probe_key_id
        and frozenset(row.excluded_key_ids) == command.excluded_key_ids
        and row.started_at == command.started_at
        and (
            command.explicit_probe_key_id is None
            or row.upstream_key_id == command.explicit_probe_key_id
        )
    )


def pending_start_matches(
    row: UpstreamAttemptReceiptRow,
    event: AdminEventRow | None,
    pin: UpstreamLivePinRow | None,
    command: AttemptStartCommand,
) -> bool:
    """Require the exact pending receipt, start event, and live pin."""
    return (
        start_matches(row, command)
        and row.terminal_outcome is None
        and event is not None
        and event.request_id == command.request_id
        and event.event_type == EventType.UPSTREAM_ATTEMPT.value
        and event.upstream_key_id == row.upstream_key_id
        and event.outcome_class == EventOutcome.STARTED.value
        and event.status_class is None
        and event.latency_ms is None
        and event.occurred_at == command.started_at
        and event.attempt_started_event_id is None
        and pin is not None
        and pin.upstream_key_id == row.upstream_key_id
        and pin.service_epoch == command.service_epoch
        and pin.pinned_at == command.started_at
    )


def identity_matches(row: UpstreamAttemptReceiptRow, identity: AttemptIdentity) -> bool:
    """Compare the complete terminal identity."""
    return (
        row.started_event_id == identity.started_event_id
        and row.terminal_event_id == identity.terminal_event_id
        and row.request_id == identity.request_id
        and row.service_epoch == identity.service_epoch
        and row.started_at == identity.started_at
    )


def terminal_matches(
    row: UpstreamAttemptReceiptRow,
    command: AttemptFinalizeCommand,
) -> bool:
    """Compare every terminal field for exact-repeat idempotency."""
    return (
        identity_matches(row, command.identity)
        and row.terminal_outcome == command.outcome.value
        and row.terminal_status_class == command.status_class.value
        and row.terminal_latency_ms == command.latency_ms
        and row.terminal_cooldown_until == command.cooldown_until
        and row.terminal_cooldown_kind
        == (None if command.cooldown_kind is None else command.cooldown_kind.value)
        and row.terminal_committed_at == command.terminal_committed_at
    )


def completed_terminal_matches(
    row: UpstreamAttemptReceiptRow,
    event: AdminEventRow | None,
    pin: UpstreamLivePinRow | None,
    command: AttemptFinalizeCommand,
) -> bool:
    """Require an exact completed receipt/event pair and no stale live pin."""
    expected_outcome = (
        EventOutcome.SUCCEEDED
        if command.outcome.value == "succeeded"
        else EventOutcome.CANCELLED
        if command.outcome.value == "cancelled"
        else EventOutcome.FAILED
    )
    return (
        terminal_matches(row, command)
        and pin is None
        and event is not None
        and event.request_id == command.identity.request_id
        and event.event_type == EventType.UPSTREAM_ATTEMPT.value
        and event.upstream_key_id == row.upstream_key_id
        and event.outcome_class == expected_outcome.value
        and event.status_class == command.status_class.value
        and event.latency_ms == command.latency_ms
        and event.occurred_at == command.terminal_committed_at
        and event.attempt_started_event_id == command.identity.started_event_id
    )


def complete_receipt(
    row: UpstreamAttemptReceiptRow,
    command: AttemptFinalizeCommand,
) -> None:
    """Fill the six terminal receipt fields once."""
    row.terminal_outcome = command.outcome.value
    row.terminal_status_class = command.status_class.value
    row.terminal_latency_ms = command.latency_ms
    row.terminal_cooldown_until = command.cooldown_until
    row.terminal_cooldown_kind = (
        None if command.cooldown_kind is None else command.cooldown_kind.value
    )
    row.terminal_committed_at = command.terminal_committed_at


def started_event(key_id: UUID, command: AttemptStartCommand) -> AdminEventRow:
    """Build the one durable started event."""
    return AdminEventRow(
        id=command.started_event_id,
        request_id=command.request_id,
        event_type=EventType.UPSTREAM_ATTEMPT.value,
        upstream_key_id=key_id,
        downstream_token_id=None,
        outcome_class=EventOutcome.STARTED.value,
        status_class=None,
        latency_ms=None,
        occurred_at=command.started_at,
        attempt_started_event_id=None,
    )


def pending_receipt(
    key_id: UUID,
    command: AttemptStartCommand,
    *,
    rate_limit_streak: int,
    transient_failure_streak: int,
) -> UpstreamAttemptReceiptRow:
    """Build the pending receipt committed before network I/O."""
    return UpstreamAttemptReceiptRow(
        started_event_id=command.started_event_id,
        terminal_event_id=command.terminal_event_id,
        upstream_key_id=key_id,
        request_id=command.request_id,
        service_epoch=command.service_epoch,
        explicit_probe_key_id=command.explicit_probe_key_id,
        excluded_key_ids=sorted(command.excluded_key_ids, key=lambda value: value.bytes),
        started_at=command.started_at,
        rate_limit_streak=rate_limit_streak,
        transient_failure_streak=transient_failure_streak,
        terminal_outcome=None,
        terminal_status_class=None,
        terminal_latency_ms=None,
        terminal_cooldown_until=None,
        terminal_cooldown_kind=None,
        terminal_committed_at=None,
    )


def live_pin(key_id: UUID, command: AttemptStartCommand) -> UpstreamLivePinRow:
    """Build the live deletion guard for the active service epoch."""
    return UpstreamLivePinRow(
        started_event_id=command.started_event_id,
        upstream_key_id=key_id,
        service_epoch=command.service_epoch,
        pinned_at=command.started_at,
    )


def terminal_event(
    row: UpstreamAttemptReceiptRow,
    command: AttemptFinalizeCommand,
) -> AdminEventRow:
    """Build the terminal event linked to exactly one started event."""
    outcome = (
        EventOutcome.SUCCEEDED
        if command.outcome.value == "succeeded"
        else EventOutcome.CANCELLED
        if command.outcome.value == "cancelled"
        else EventOutcome.FAILED
    )
    return AdminEventRow(
        id=row.terminal_event_id,
        request_id=row.request_id,
        event_type=EventType.UPSTREAM_ATTEMPT.value,
        upstream_key_id=row.upstream_key_id,
        downstream_token_id=None,
        outcome_class=outcome.value,
        status_class=command.status_class.value,
        latency_ms=command.latency_ms,
        occurred_at=command.terminal_committed_at,
        attempt_started_event_id=row.started_event_id,
    )
