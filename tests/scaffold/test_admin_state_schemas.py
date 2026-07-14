import json

import pytest
from pydantic import ValidationError

from nvidia_build_lb.admin.schemas import (
    AdminEventListResponse,
    AdminEventRead,
    AdminOverviewRead,
    AdminValidationErrorResponse,
    EventOutcome,
    EventType,
    OverviewStatus,
)


def test_admin_overview_has_the_exact_aggregate_shape() -> None:
    # Given: the exact approved overview response.
    raw = {
        "status": "ok",
        "ready": True,
        "upstream_keys": {"total": 2, "enabled": 2, "eligible": 1, "cooling": 1, "degraded": 1},
        "downstream_tokens": {"total": 3, "active": 2, "revoked": 1},
        "request_count": 42,
        "last_event_at": "2026-07-12T01:02:03Z",
        "generated_at": "2026-07-12T01:02:04Z",
    }

    # When: it crosses the administration response boundary.
    response = AdminOverviewRead.model_validate_json(json.dumps(raw))

    # Then: every nested field is present and canonical.
    assert response.model_dump(mode="json") == raw
    assert response.status is OverviewStatus.OK


@pytest.mark.parametrize("bad_count", [-1, 1.5, True])
def test_admin_overview_rejects_invalid_counters(bad_count: float | bool) -> None:
    # Given: an overview with one non-counter value.
    raw = {
        "status": "degraded",
        "ready": False,
        "upstream_keys": {"total": 0, "enabled": 0, "eligible": 0, "cooling": 0, "degraded": 0},
        "downstream_tokens": {"total": 0, "active": 0, "revoked": 0},
        "request_count": bad_count,
        "last_event_at": None,
        "generated_at": "2026-07-12T01:02:04Z",
    }

    # When: it crosses the administration response boundary.
    with pytest.raises(ValidationError) as captured:
        _ = AdminOverviewRead.model_validate_json(json.dumps(raw))

    # Then: the counter is rejected.
    assert captured.value.error_count() == 1


def test_admin_event_and_list_have_exact_safe_shapes() -> None:
    # Given: one durable pre-network attempt event.
    raw = {
        "id": "00000000-0000-4000-8000-00000000abcd",
        "request_id": "opaque-request",
        "event_type": "upstream_attempt",
        "upstream_key_id": "00000000-0000-4000-8000-000000000001",
        "downstream_token_id": None,
        "outcome_class": "started",
        "status_class": None,
        "latency_ms": None,
        "occurred_at": "2026-07-12T01:02:03Z",
    }

    # When: the item and collection cross their response boundaries.
    item = AdminEventRead.model_validate_json(json.dumps(raw))
    collection = AdminEventListResponse.model_validate_json(json.dumps({"items": [raw]}))

    # Then: only the exact safe event fields remain.
    assert item.model_dump(mode="json") == raw
    assert collection.items == (item,)


def test_admin_event_rejects_non_utc_timestamp_and_negative_latency() -> None:
    # Given: a safe event shape with two invalid boundary values.
    raw = {
        "id": "00000000-0000-4000-8000-00000000abcd",
        "request_id": "opaque-request",
        "event_type": "upstream_attempt",
        "upstream_key_id": None,
        "downstream_token_id": None,
        "outcome_class": "failed",
        "status_class": "timeout",
        "latency_ms": -1,
        "occurred_at": "2026-07-12T03:02:03+02:00",
    }

    # When: it crosses the response boundary.
    with pytest.raises(ValidationError) as captured:
        _ = AdminEventRead.model_validate_json(json.dumps(raw))

    # Then: both invalid fields are rejected.
    assert captured.value.error_count() == 2


def test_admin_validation_error_is_fixed_and_extra_forbidden() -> None:
    # Given: the only safe validation response and an unsafe extra-detail variant.
    raw = {
        "error": {
            "code": "invalid_request",
            "message": "request validation failed",
            "request_id": "opaque-request",
        }
    }
    unsafe = {"error": {**raw["error"], "input": "rejected-value"}}

    # When: both shapes cross the common error response boundary.
    response = AdminValidationErrorResponse.model_validate_json(json.dumps(raw))
    with pytest.raises(ValidationError) as captured:
        _ = AdminValidationErrorResponse.model_validate_json(json.dumps(unsafe))

    # Then: only the fixed safe shape is accepted.
    assert response.model_dump(mode="json") == raw
    assert captured.value.error_count() == 1


def test_admin_event_enums_are_exactly_closed() -> None:
    # Given: approved event and outcome values.
    expected_types = {
        "upstream_key_created",
        "upstream_key_enabled",
        "upstream_key_disabled",
        "upstream_key_deleted",
        "upstream_probe",
        "downstream_token_issued",
        "downstream_token_revoked",
        "upstream_attempt",
    }
    expected_outcomes = {"started", "succeeded", "failed", "cancelled"}

    # When: consumers enumerate both closed sets.
    event_types = {item.value for item in EventType}
    outcomes = {item.value for item in EventOutcome}

    # Then: no event field value is added or omitted.
    assert event_types == expected_types
    assert outcomes == expected_outcomes
