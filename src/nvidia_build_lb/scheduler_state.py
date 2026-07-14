"""Durable scheduler cursor and logical-attempt transaction types."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from nvidia_build_lb.admin.schemas import EventOutcome, EventType
from nvidia_build_lb.credential_types import ResourceConflictError, ResourceNotFoundError
from nvidia_build_lb.db_models import AdminEventRow, SchedulerStateRow, UpstreamKeyRow
from nvidia_build_lb.quarantine_recovery import terminal_updates_routing_state
from nvidia_build_lb.scheduler_lock import lock_scheduler_state
from nvidia_build_lb.scheduler_outcomes import outcome_change
from nvidia_build_lb.scheduler_ring import next_ring_key
from nvidia_build_lb.scheduler_transitions_legacy import apply_legacy_terminal_transition
from nvidia_build_lb.scheduler_types import (
    AttemptLease,
    AttemptTerminal,
    LockedAttempt,
    NoEligibleUpstreamKeyError,
    SchedulerDependencies,
    SchedulerStateUnavailableError,
    TerminalOutcome,
)

__all__ = [
    "AttemptLease",
    "AttemptTerminal",
    "NoEligibleUpstreamKeyError",
    "SchedulerDependencies",
    "SchedulerStateRepository",
    "SchedulerStateUnavailableError",
    "TerminalOutcome",
]


@dataclass(frozen=True, slots=True)
class SchedulerStateRepository:
    """Own pre-network and terminal transactions without NVIDIA I/O."""

    dependencies: SchedulerDependencies

    async def begin_next_attempt(self, request_id: str) -> AttemptLease:
        """Select and commit the next eligible key in one locked transaction."""
        now = self.dependencies.clock.now()
        async with self.dependencies.sessions.begin() as session:
            scheduler = await lock_scheduler_state(session)
            rows = tuple(
                (
                    await session.scalars(
                        select(UpstreamKeyRow).order_by(
                            UpstreamKeyRow.created_at.asc(), UpstreamKeyRow.id.asc()
                        )
                    )
                ).all()
            )
            eligible = tuple(
                row
                for row in rows
                if row.enabled
                and not row.quarantined
                and (row.cooldown_until is None or row.cooldown_until <= now)
            )
            if not eligible:
                raise NoEligibleUpstreamKeyError
            selected = next_ring_key(rows, eligible, scheduler.cursor_key_id)
            key = await session.get(UpstreamKeyRow, selected.id, with_for_update=True)
            if key is None:
                raise ResourceNotFoundError(resource="upstream_key", resource_id=selected.id)
            return _start_attempt(
                LockedAttempt(session, scheduler, key),
                request_id,
                now,
                explicit_probe=False,
            )

    async def begin_attempt(self, key_id: UUID, request_id: str) -> AttemptLease:
        """Commit an explicit-key admin probe attempt before its network I/O."""
        now = self.dependencies.clock.now()
        async with self.dependencies.sessions.begin() as session:
            scheduler = await lock_scheduler_state(session)
            key = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
            if key is None:
                raise ResourceNotFoundError(resource="upstream_key", resource_id=key_id)
            return _start_attempt(
                LockedAttempt(session, scheduler, key),
                request_id,
                now,
                explicit_probe=True,
            )

    async def current_cursor(self) -> UUID | None:
        """Read the persisted cursor without modifying scheduler state."""
        async with self.dependencies.sessions() as session:
            scheduler = await session.get(SchedulerStateRow, 1)
        if scheduler is None:
            raise SchedulerStateUnavailableError
        return scheduler.cursor_key_id

    async def finish_attempt(self, lease: AttemptLease, terminal: AttemptTerminal) -> None:
        """Commit one terminal counter, event, and safe routing state once."""
        now = self.dependencies.clock.now()
        async with self.dependencies.sessions.begin() as session:
            _ = await lock_scheduler_state(session)
            key = await session.get(UpstreamKeyRow, lease.key_id, with_for_update=True)
            if key is None:
                raise ResourceNotFoundError(
                    resource="upstream_key",
                    resource_id=lease.key_id,
                )
            started = await session.get(AdminEventRow, lease.started_event_id, with_for_update=True)
            if (
                started is None
                or started.request_id != lease.request_id
                or started.upstream_key_id != lease.key_id
                or started.event_type != EventType.UPSTREAM_ATTEMPT.value
                or started.outcome_class != EventOutcome.STARTED.value
            ):
                raise ResourceConflictError(resource="upstream_attempt")
            prior_terminal = await session.scalar(
                select(AdminEventRow.id)
                .where(
                    AdminEventRow.request_id == lease.request_id,
                    AdminEventRow.upstream_key_id == lease.key_id,
                    AdminEventRow.event_type == EventType.UPSTREAM_ATTEMPT.value,
                    AdminEventRow.outcome_class != EventOutcome.STARTED.value,
                )
                .limit(1)
            )
            if prior_terminal is not None:
                raise ResourceConflictError(resource="upstream_attempt")
            change = outcome_change(terminal.outcome)
            key.success_count += change.success_delta
            key.failure_count += change.failure_delta
            apply_legacy_terminal_transition(
                key,
                terminal,
                explicit_probe=lease.explicit_probe,
            )
            if terminal_updates_routing_state(
                key,
                terminal.status_class,
                explicit_probe=lease.explicit_probe,
            ):
                key.last_status_class = terminal.status_class.value
            key.updated_at = now
            session.add(
                AdminEventRow(
                    id=uuid4(),
                    request_id=lease.request_id,
                    event_type=EventType.UPSTREAM_ATTEMPT.value,
                    upstream_key_id=lease.key_id,
                    downstream_token_id=None,
                    outcome_class=change.event_outcome.value,
                    status_class=terminal.status_class.value,
                    latency_ms=terminal.latency_ms,
                    occurred_at=now,
                )
            )


def _start_attempt(
    locked: LockedAttempt,
    request_id: str,
    now: datetime,
    *,
    explicit_probe: bool,
) -> AttemptLease:
    event_id = uuid4()
    locked.key.request_count += 1
    locked.key.last_used_at = now
    locked.key.updated_at = now
    locked.scheduler.cursor_key_id = locked.key.id
    locked.scheduler.updated_at = now
    locked.session.add(
        AdminEventRow(
            id=event_id,
            request_id=request_id,
            event_type=EventType.UPSTREAM_ATTEMPT.value,
            upstream_key_id=locked.key.id,
            downstream_token_id=None,
            outcome_class=EventOutcome.STARTED.value,
            status_class=None,
            latency_ms=None,
            occurred_at=now,
        )
    )
    return AttemptLease(locked.key.id, request_id, event_id, now, explicit_probe)
