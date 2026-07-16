import json

import pytest
from pydantic import JsonValue, ValidationError

from nvidia_build_lb.admin.schemas import (
    HealthState,
    LastStatusClass,
    ProbeStatus,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
    UpstreamProbeResponse,
    UpstreamRoutingState,
)


def test_upstream_key_request_preserves_exact_utf8_input() -> None:
    # Given: a strict string whose surrounding and Unicode bytes are significant.
    candidate = "  opaque.자격값  "

    # When: the credential crosses the admin request boundary.
    request = UpstreamKeyCreateRequest.model_validate({"key": candidate})

    # Then: no trimming, normalization, or provider-format rewrite occurs.
    assert request.key == candidate


@pytest.mark.parametrize("candidate", ["a", "é" * 2048])
def test_upstream_key_request_accepts_utf8_byte_boundaries(candidate: str) -> None:
    # Given: an opaque string at an accepted UTF-8 byte boundary.
    payload = {"key": candidate}

    # When: the request is parsed.
    request = UpstreamKeyCreateRequest.model_validate(payload)

    # Then: the exact string remains available for fingerprinting and encryption.
    assert request.key == candidate


@pytest.mark.parametrize(
    "candidate",
    ["", "é" * 2049, "prefix\x00suffix", "prefix\rsuffix", "prefix\nsuffix", "\ud800", 7],
)
def test_upstream_key_request_rejects_invalid_opaque_values(candidate: JsonValue) -> None:
    # Given: one value outside the strict opaque credential contract.
    payload = {"key": candidate}

    # When: it crosses the request boundary.
    with pytest.raises(ValidationError) as captured:
        _ = UpstreamKeyCreateRequest.model_validate(payload)

    # Then: validation fails without asserting against the rejected value.
    assert captured.value.error_count() == 1


def test_upstream_key_request_forbids_unknown_fields() -> None:
    # Given: an otherwise valid request with one undeclared field.
    payload = {"key": "opaque-value", "enabled": True}

    # When: it crosses the strict request boundary.
    with pytest.raises(ValidationError) as captured:
        _ = UpstreamKeyCreateRequest.model_validate(payload)

    # Then: the extra field is rejected.
    assert captured.value.error_count() == 1


def test_upstream_key_read_has_the_exact_safe_wire_shape() -> None:
    # Given: a safe persisted row encoded as administration JSON.
    raw = {
        "id": "00000000-0000-4000-8000-00000000ABCD",
        "fingerprint": f"sha256:{'a' * 64}",
        "enabled": False,
        "routing_state": "disabled",
        "health_state": "unknown",
        "cooldown_until": None,
        "request_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "last_status_class": None,
        "last_used_at": None,
        "created_at": "2026-07-12T01:02:03Z",
        "updated_at": "2026-07-12T01:02:03Z",
    }

    # When: it crosses the response boundary.
    response = UpstreamKeyRead.model_validate_json(json.dumps(raw))

    # Then: serialization is canonical, complete, and secret-free.
    expected = dict(raw)
    expected["id"] = "00000000-0000-4000-8000-00000000abcd"
    assert response.model_dump(mode="json") == expected


@pytest.mark.parametrize(
    "fingerprint",
    [f"sha256:{'A' * 64}", f"sha256:{'a' * 63}", "digest:" + "a" * 64],
)
def test_upstream_key_read_rejects_noncanonical_fingerprints(fingerprint: str) -> None:
    # Given: an otherwise shaped row with a noncanonical wire fingerprint.
    raw = {
        "id": "00000000-0000-4000-8000-00000000abcd",
        "fingerprint": fingerprint,
        "enabled": False,
        "routing_state": "disabled",
        "health_state": "unknown",
        "cooldown_until": None,
        "request_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "last_status_class": None,
        "last_used_at": None,
        "created_at": "2026-07-12T01:02:03Z",
        "updated_at": "2026-07-12T01:02:03Z",
    }

    # When: it crosses the response boundary.
    with pytest.raises(ValidationError) as captured:
        _ = UpstreamKeyRead.model_validate_json(json.dumps(raw))

    # Then: the fingerprint is rejected as one field error.
    assert captured.value.error_count() == 1


def test_upstream_list_and_probe_have_closed_shapes() -> None:
    # Given: exact empty-list and valid-probe administration payloads.
    list_json = '{"items":[]}'
    probe_json = (
        '{"id":"00000000-0000-4000-8000-00000000abcd",'
        '"enabled":false,"probe_status":"valid",'
        '"observed_at":"2026-07-12T01:02:03Z"}'
    )

    # When: both response shapes are parsed.
    key_list = UpstreamKeyListResponse.model_validate_json(list_json)
    probe = UpstreamProbeResponse.model_validate_json(probe_json)

    # Then: list and probe expose only their approved values.
    assert key_list.items == ()
    assert probe.probe_status is ProbeStatus.VALID
    assert probe.enabled is False


def test_upstream_admin_enums_are_exactly_closed() -> None:
    # Given: the approved route-visible upstream states.
    expected_health = {"unknown", "healthy", "degraded"}
    expected_probe = {"valid", "invalid_credential", "rate_limited", "upstream_unavailable"}
    expected_routing = {"disabled", "eligible", "cooldown", "quarantined"}
    expected_status = {
        "success",
        "invalid_credential",
        "credits_exhausted",
        "rate_limited",
        "request_rejected",
        "timeout",
        "upstream_unavailable",
        "upstream_bad_gateway",
        "upstream_internal_error",
        "upstream_protocol_error",
        "delivery_failed",
        "cancelled",
    }

    # When: consumers enumerate the closed wire values.
    health = {item.value for item in HealthState}
    probe = {item.value for item in ProbeStatus}
    routing = {item.value for item in UpstreamRoutingState}
    status = {item.value for item in LastStatusClass}

    # Then: no routing or probe state is added or omitted.
    assert health == expected_health
    assert probe == expected_probe
    assert routing == expected_routing
    assert status == expected_status
