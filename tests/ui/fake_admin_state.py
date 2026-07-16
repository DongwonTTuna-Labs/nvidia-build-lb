from dataclasses import dataclass
from threading import Lock
from typing import Literal, final
from uuid import UUID

from nvidia_build_lb.admin.schemas import (
    AdminDashboardEventListResponse,
    AdminDashboardRead,
    AdminEventListResponse,
    AdminLedgerRead,
    AdminOverviewRead,
    CapacityBlocker,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    LedgerStatus,
    ReadinessCause,
    RuntimeState,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)

from .fake_admin_models import BASE_TIME, FakeAdminData, FakeAdminError
from .fake_admin_tokens import issue_token, revoke_token
from .fake_admin_upstreams import UpstreamAction, add_upstream, build_overview, change_upstream

type BoundaryStep = Literal[
    "host",
    "origin",
    "auth",
    "no_op",
    "options_405",
    "route:dashboard",
    "route:overview",
    "route:upstream_list",
    "route:upstream_create",
    "route:upstream_enable",
    "route:upstream_disable",
    "route:upstream_probe",
    "route:upstream_delete",
    "route:downstream_list",
    "route:downstream_issue",
    "route:downstream_revoke",
    "route:events",
]

__all__ = (
    "BoundaryStep",
    "FakeAdminError",
    "FakeAdminState",
    "SafeNetworkObservation",
)


@dataclass(frozen=True, slots=True)
class SafeNetworkObservation:
    method: str
    path: str
    status: int
    query_present: bool


@final
class FakeAdminState:
    """Mutable in-memory fixture whose sole purpose is deterministic browser mutation."""

    __slots__ = (
        "_boundary_steps",
        "_data",
        "_failure_path",
        "_lock",
        "_network_audit",
        "admin_bearer",
        "downstream_bearer",
        "issued_bearer",
    )

    def __init__(self) -> None:
        self._lock = Lock()
        self.admin_bearer = "nblb_admin_" + ("1" * 64)
        self.downstream_bearer = "nblb_ds_" + ("2" * 64)
        self.issued_bearer = "nblb_ds_" + ("3" * 64)
        self._failure_path: str | None = None
        self._boundary_steps: list[BoundaryStep] = []
        self._network_audit: list[SafeNetworkObservation] = []
        self._data = FakeAdminData()

    def reset(self) -> None:
        """Restore the same safe populated state before each browser journey."""
        with self._lock:
            self._failure_path = None
            self._boundary_steps = []
            self._data.reset()

    def set_empty(self) -> None:
        with self._lock:
            self._data.set_empty()

    def record_boundary(self, step: BoundaryStep) -> None:
        with self._lock:
            self._boundary_steps.append(step)

    def reset_boundary_audit(self) -> None:
        with self._lock:
            self._boundary_steps = []

    def boundary_audit(self) -> tuple[BoundaryStep, ...]:
        with self._lock:
            return tuple(self._boundary_steps)

    def record_network(self, observation: SafeNetworkObservation) -> None:
        with self._lock:
            self._network_audit.append(observation)

    def reset_network_audit(self) -> None:
        with self._lock:
            self._network_audit = []

    def network_audit(self) -> tuple[SafeNetworkObservation, ...]:
        with self._lock:
            return tuple(self._network_audit)

    def fail_next(self, path: str) -> None:
        """Fail the next exact API path with a safe unavailable result."""
        with self._lock:
            self._failure_path = path

    def check_available(self, path: str) -> None:
        """Consume one configured deterministic database failure."""
        with self._lock:
            if self._failure_path == path:
                self._failure_path = None
                raise FakeAdminError(
                    503, "database_unavailable", "administration state unavailable"
                )

    def overview(self) -> AdminOverviewRead:
        """Return exact aggregates over the current fake rows."""
        with self._lock:
            return build_overview(self._data)

    def dashboard(self) -> AdminDashboardRead:
        """Return one strict coherent browser snapshot from the locked fake state."""
        with self._lock:
            overview = build_overview(self._data)
            events = AdminDashboardEventListResponse(items=tuple(self._data.events[:100]))
            return AdminDashboardRead(
                runtime_state=RuntimeState.OPERATIONAL,
                readiness_cause=(
                    ReadinessCause.READY if overview.ready else ReadinessCause.NO_ELIGIBLE_UPSTREAM
                ),
                ledger=AdminLedgerRead(
                    status=LedgerStatus.OK,
                    capacity_blocker=CapacityBlocker.NONE,
                    event_rows=len(self._data.events),
                    reserved_terminal_slots=0,
                    event_capacity=10_000,
                    attempt_rows=0,
                    attempt_capacity=5_000,
                    last_maintenance_completed_at=BASE_TIME,
                    last_pruned_event_rows=0,
                    last_pruned_attempt_rows=0,
                    oldest_event_at=(
                        self._data.events[-1].occurred_at if self._data.events else None
                    ),
                ),
                overview=overview,
                upstream_keys=UpstreamKeyListResponse(items=tuple(self._data.upstreams)),
                downstream_tokens=DownstreamTokenListResponse(items=tuple(self._data.tokens)),
                events=events,
            )

    def upstreams(self) -> UpstreamKeyListResponse:
        """Return the current ordered safe upstream collection."""
        with self._lock:
            return UpstreamKeyListResponse(items=tuple(self._data.upstreams))

    def add_upstream(self, request: UpstreamKeyCreateRequest) -> UpstreamKeyRead:
        """Fingerprint and discard one synthetic credential before adding a disabled row."""
        with self._lock:
            return add_upstream(self._data, request)

    def change_upstream(
        self,
        item_id: UUID,
        action: UpstreamAction,
    ) -> UpstreamProbeResponse | None:
        """Apply one exact enable, disable, delete, or probe transition."""
        with self._lock:
            return change_upstream(self._data, item_id, action)

    def tokens(self) -> DownstreamTokenListResponse:
        """Return active and revoked digest-free token rows."""
        with self._lock:
            return DownstreamTokenListResponse(items=tuple(self._data.tokens))

    def issue_token(self, request: DownstreamTokenIssueRequest) -> DownstreamTokenIssued:
        """Return one synthetic plaintext token while persisting only safe list state."""
        with self._lock:
            return issue_token(self._data, request, self.issued_bearer)

    def revoke_token(self, item_id: UUID) -> None:
        """Revoke one active fake token and reject repeated revocation."""
        with self._lock:
            revoke_token(self._data, item_id)

    def events(self) -> AdminEventListResponse:
        """Return newest-first safe fake events."""
        with self._lock:
            return AdminEventListResponse(items=tuple(self._data.events[:100]))
