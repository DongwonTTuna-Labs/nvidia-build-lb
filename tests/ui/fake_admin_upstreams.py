from hashlib import sha256
from typing import Literal, assert_never
from uuid import UUID

from nvidia_build_lb.admin.schemas import (
    AdminOverviewRead,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    OverviewStatus,
    ProbeStatus,
    UpstreamKeyCreateRequest,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)

from .fake_admin_models import (
    BASE_TIME,
    FakeAdminData,
    FakeAdminError,
    FakeEventSpec,
    fake_uuid,
)

type UpstreamAction = Literal["enable", "disable", "delete", "probe"]


def build_overview(data: FakeAdminData) -> AdminOverviewRead:
    enabled = sum(item.enabled for item in data.upstreams)
    cooling = sum(item.cooldown_until is not None for item in data.upstreams)
    degraded = sum(item.health_state is HealthState.DEGRADED for item in data.upstreams)
    eligible = sum(item.enabled and item.cooldown_until is None for item in data.upstreams)
    revoked = sum(item.revoked_at is not None for item in data.tokens)
    status = OverviewStatus.OK if eligible else OverviewStatus.DEGRADED
    return AdminOverviewRead.model_validate(
        {
            "status": status,
            "ready": bool(eligible),
            "upstream_keys": {
                "total": len(data.upstreams),
                "enabled": enabled,
                "eligible": eligible,
                "cooling": cooling,
                "degraded": degraded,
            },
            "downstream_tokens": {
                "total": len(data.tokens),
                "active": len(data.tokens) - revoked,
                "revoked": revoked,
            },
            "request_count": sum(item.request_count for item in data.upstreams),
            "last_event_at": data.events[0].occurred_at if data.events else None,
            "generated_at": BASE_TIME,
        }
    )


def add_upstream(data: FakeAdminData, request: UpstreamKeyCreateRequest) -> UpstreamKeyRead:
    fingerprint = "sha256:" + sha256(request.key.encode()).hexdigest()
    if any(item.fingerprint == fingerprint for item in data.upstreams):
        raise FakeAdminError(409, "resource_conflict", "upstream key already exists")
    item = UpstreamKeyRead(
        id=fake_uuid(data.upstream_sequence),
        fingerprint=fingerprint,
        enabled=False,
        health_state=HealthState.UNKNOWN,
        cooldown_until=None,
        request_count=0,
        success_count=0,
        failure_count=0,
        last_status_class=None,
        last_used_at=None,
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    data.upstream_sequence += 1
    data.upstreams.append(item)
    data.events.insert(
        0,
        data.record_event(
            FakeEventSpec(
                event_type=EventType.UPSTREAM_KEY_CREATED,
                outcome=EventOutcome.SUCCEEDED,
                upstream_id=item.id,
                token_id=None,
            )
        ),
    )
    return item


def change_upstream(
    data: FakeAdminData,
    item_id: UUID,
    action: UpstreamAction,
) -> UpstreamProbeResponse | None:
    index = next(
        (offset for offset, item in enumerate(data.upstreams) if item.id == item_id),
        None,
    )
    if index is None:
        raise FakeAdminError(404, "resource_not_found", "upstream key not found")
    item = data.upstreams[index]
    match action:
        case "enable" | "disable":
            enabled = action == "enable"
            data.upstreams[index] = item.model_copy(
                update={"enabled": enabled, "updated_at": BASE_TIME}
            )
            event_type = (
                EventType.UPSTREAM_KEY_ENABLED if enabled else EventType.UPSTREAM_KEY_DISABLED
            )
            data.events.insert(
                0,
                data.record_event(FakeEventSpec(event_type, EventOutcome.SUCCEEDED, item.id, None)),
            )
            return None
        case "delete":
            if item.enabled:
                raise FakeAdminError(
                    409,
                    "resource_conflict",
                    "enabled upstream key cannot be deleted",
                )
            del data.upstreams[index]
            data.events.insert(
                0,
                data.record_event(
                    FakeEventSpec(
                        EventType.UPSTREAM_KEY_DELETED,
                        EventOutcome.SUCCEEDED,
                        item.id,
                        None,
                    )
                ),
            )
            return None
        case "probe":
            data.upstreams[index] = item.model_copy(
                update={
                    "health_state": HealthState.HEALTHY,
                    "cooldown_until": None,
                    "last_status_class": LastStatusClass.SUCCESS,
                    "updated_at": BASE_TIME,
                }
            )
            data.events.insert(
                0,
                data.record_event(
                    FakeEventSpec(
                        EventType.UPSTREAM_PROBE,
                        EventOutcome.SUCCEEDED,
                        item.id,
                        None,
                    )
                ),
            )
            return UpstreamProbeResponse(
                id=item.id,
                enabled=item.enabled,
                probe_status=ProbeStatus.VALID,
                observed_at=BASE_TIME,
            )
        case _:
            assert_never(action)
