"""Pure projections for the coherent administration dashboard snapshot."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from nvidia_build_lb.admin.schemas import (
    AdminDashboardEventListResponse,
    AdminLedgerRead,
    AdminOverviewRead,
    CapacityBlocker,
    DownstreamTokenListResponse,
    DownstreamTokenOverview,
    LedgerStatus,
    OverviewStatus,
    UpstreamKeyListResponse,
    UpstreamKeyOverview,
)
from nvidia_build_lb.admin_ledger import AdminLedgerPolicy, LedgerCounts
from nvidia_build_lb.db_models import AdminLedgerStateRow


@dataclass(frozen=True, slots=True)
class LedgerProjectionInput:
    """Inputs needed to project ledger state for one snapshot."""

    state: AdminLedgerStateRow
    counts: LedgerCounts
    policy: AdminLedgerPolicy
    as_of: datetime
    oldest_event_at: datetime | None
    capacity_available: bool


@dataclass(frozen=True, slots=True)
class OverviewProjectionInput:
    """Inputs needed to project operator overview state for one snapshot."""

    upstream: UpstreamKeyListResponse
    downstream: DownstreamTokenListResponse
    events: AdminDashboardEventListResponse
    rolled_up: int
    retained: int
    generated_at: datetime
    capacity_available: bool


@dataclass(frozen=True, slots=True)
class LedgerCondition:
    """Closed ledger status shared by full and bounded operator projections."""

    status: LedgerStatus
    capacity_blocker: CapacityBlocker


def ledger_condition(
    row: AdminLedgerStateRow,
    policy: AdminLedgerPolicy,
    as_of: datetime,
    *,
    capacity_available: bool,
) -> LedgerCondition:
    """Project capacity and maintenance truth without reading retained collections."""
    persisted_blocker = CapacityBlocker(row.last_capacity_blocker)
    if capacity_available:
        blocker = CapacityBlocker.NONE
        overdue = as_of - row.last_maintenance_completed_at > timedelta(
            seconds=2 * policy.maintenance_interval_seconds
        )
        status = LedgerStatus.MAINTENANCE_OVERDUE if overdue else LedgerStatus.OK
    else:
        blocker = persisted_blocker
        status = (
            LedgerStatus.CAPACITY_BLOCKED
            if blocker in {CapacityBlocker.ORPHANED_PENDING, CapacityBlocker.LEGACY_UNLINKED}
            else LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING
        )
    return LedgerCondition(status=status, capacity_blocker=blocker)


def ledger_read(snapshot: LedgerProjectionInput) -> AdminLedgerRead:
    """Project persisted ledger facts into the admin read schema."""
    row = snapshot.state
    counts = snapshot.counts
    policy = snapshot.policy
    condition = ledger_condition(
        row,
        policy,
        snapshot.as_of,
        capacity_available=snapshot.capacity_available,
    )
    return AdminLedgerRead(
        status=condition.status,
        capacity_blocker=condition.capacity_blocker,
        event_rows=counts.event_rows,
        reserved_terminal_slots=counts.pending_receipts,
        event_capacity=policy.event_max_rows,
        attempt_rows=counts.receipt_rows,
        attempt_capacity=policy.attempt_max_rows,
        last_maintenance_completed_at=row.last_maintenance_completed_at,
        last_pruned_event_rows=row.last_pruned_event_rows,
        last_pruned_attempt_rows=row.last_pruned_attempt_rows,
        oldest_event_at=snapshot.oldest_event_at,
    )


def overview_read(snapshot: OverviewProjectionInput) -> AdminOverviewRead:
    """Project resource counts and readiness into the overview schema."""
    key_items = snapshot.upstream.items
    token_items = snapshot.downstream.items
    eligible = sum(item.routing_state.value == "eligible" for item in key_items)
    cooling = sum(item.routing_state.value == "cooldown" for item in key_items)
    degraded = sum(item.health_state.value == "degraded" for item in key_items)
    active = sum(item.revoked_at is None for item in token_items)
    ready = snapshot.capacity_available and eligible > 0
    return AdminOverviewRead(
        status=OverviewStatus.OK if ready else OverviewStatus.DEGRADED,
        ready=ready,
        upstream_keys=UpstreamKeyOverview(
            total=len(key_items),
            enabled=sum(item.enabled for item in key_items),
            eligible=eligible,
            cooling=cooling,
            degraded=degraded,
        ),
        downstream_tokens=DownstreamTokenOverview(
            total=len(token_items),
            active=active,
            revoked=len(token_items) - active,
        ),
        request_count=snapshot.rolled_up + snapshot.retained,
        last_event_at=(None if not snapshot.events.items else snapshot.events.items[0].occurred_at),
        generated_at=snapshot.generated_at,
    )
