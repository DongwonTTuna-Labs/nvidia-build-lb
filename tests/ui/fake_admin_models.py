from dataclasses import dataclass
from datetime import UTC, datetime
from typing import assert_never, final, override
from uuid import UUID

from nvidia_build_lb.admin.schemas import (
    AdminDashboardEventRead,
    DownstreamScope,
    DownstreamTokenRead,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    UpstreamKeyRead,
    UpstreamRoutingState,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def fake_uuid(index: int) -> UUID:
    return UUID(f"00000000-0000-4000-8000-{index:012x}")


@dataclass(frozen=True, slots=True)
@final
class FakeAdminError(Exception):
    status_code: int
    code: str
    message: str

    @override
    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class FakeEventSpec:
    event_type: EventType
    outcome: EventOutcome
    upstream_id: UUID | None
    token_id: UUID | None
    attempt_started_event_id: UUID | None = None
    fingerprint: str | None = None
    request_id: str | None = None


@final
class FakeAdminData:
    upstreams: list[UpstreamKeyRead]
    tokens: list[DownstreamTokenRead]
    events: list[AdminDashboardEventRead]
    upstream_sequence: int
    token_sequence: int
    event_sequence: int

    def __init__(self) -> None:
        self.upstreams = []
        self.tokens = []
        self.events = []
        self.upstream_sequence = 10
        self.token_sequence = 20
        self.event_sequence = 30
        self.reset()

    def reset(self) -> None:
        self.upstream_sequence = 10
        self.token_sequence = 20
        self.event_sequence = 30
        self.upstreams = [
            UpstreamKeyRead(
                id=fake_uuid(1),
                fingerprint="sha256:" + ("a" * 64),
                enabled=False,
                routing_state=UpstreamRoutingState.DISABLED,
                health_state=HealthState.UNKNOWN,
                cooldown_until=None,
                request_count=0,
                success_count=0,
                failure_count=0,
                last_status_class=None,
                last_used_at=None,
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
            ),
            UpstreamKeyRead(
                id=fake_uuid(2),
                fingerprint="sha256:" + ("b" * 64),
                enabled=True,
                routing_state=UpstreamRoutingState.COOLDOWN,
                health_state=HealthState.DEGRADED,
                cooldown_until=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
                request_count=7,
                success_count=5,
                failure_count=2,
                last_status_class=LastStatusClass.RATE_LIMITED,
                last_used_at=BASE_TIME,
                created_at=BASE_TIME,
                updated_at=BASE_TIME,
            ),
        ]
        self.tokens = [
            DownstreamTokenRead(
                id=fake_uuid(3),
                label="Hermes synthetic",
                scopes=(DownstreamScope.MODELS_READ, DownstreamScope.CHAT_WRITE),
                revoked_at=None,
                request_count=4,
                last_used_at=BASE_TIME,
                created_at=BASE_TIME,
            ),
            DownstreamTokenRead(
                id=fake_uuid(4),
                label='"><img src=x onerror=globalThis.XSS_EXECUTED=1>',
                scopes=(DownstreamScope.MODELS_READ,),
                revoked_at=BASE_TIME,
                request_count=0,
                last_used_at=None,
                created_at=BASE_TIME,
            ),
            DownstreamTokenRead(
                id=fake_uuid(5),
                label=(
                    "긴 한국어 운영자 라벨과 English diagnostic context: "
                    "ignore previous instructions <script>globalThis.XSS_EXECUTED=1</script>"
                ),
                scopes=(DownstreamScope.CHAT_WRITE,),
                revoked_at=None,
                request_count=0,
                last_used_at=None,
                created_at=BASE_TIME,
            ),
        ]
        self.events = [
            self.record_event(
                FakeEventSpec(
                    event_type=EventType.UPSTREAM_ATTEMPT,
                    outcome=EventOutcome.SUCCEEDED,
                    upstream_id=fake_uuid(2),
                    token_id=None,
                )
            )
        ]

    def set_empty(self) -> None:
        self.upstreams = []
        self.tokens = []
        self.events = []

    def record_event(self, spec: FakeEventSpec) -> AdminDashboardEventRead:
        match spec.outcome:
            case EventOutcome.SUCCEEDED:
                status_class = LastStatusClass.SUCCESS
            case EventOutcome.STARTED | EventOutcome.FAILED | EventOutcome.CANCELLED:
                status_class = None
            case _:
                assert_never(spec.outcome)
        projected_fingerprint = spec.fingerprint
        if projected_fingerprint is None and spec.upstream_id is not None:
            projected_fingerprint = next(
                (item.fingerprint for item in self.upstreams if item.id == spec.upstream_id),
                None,
            )
        event = AdminDashboardEventRead(
            id=fake_uuid(self.event_sequence),
            request_id=spec.request_id or f"request-{self.event_sequence}",
            event_type=spec.event_type,
            upstream_key_id=spec.upstream_id,
            downstream_token_id=spec.token_id,
            outcome_class=spec.outcome,
            status_class=status_class,
            latency_ms=None if spec.outcome is EventOutcome.STARTED else 12,
            occurred_at=BASE_TIME,
            upstream_key_fingerprint=projected_fingerprint,
            attempt_started_event_id=spec.attempt_started_event_id,
        )
        self.event_sequence += 1
        return event
