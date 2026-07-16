import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest
from pydantic import JsonValue, TypeAdapter

pytestmark = pytest.mark.ui_fake

_ROOT: Final = Path(__file__).parents[2]
_ADMIN_SCRIPT: Final = _ROOT / "src/nvidia_build_lb/web/static/admin.js"
_BOOL_LIST: Final = TypeAdapter(list[bool])
_node = shutil.which("node")
if _node is None:
    raise RuntimeError
NODE_EXECUTABLE: Final = _node
_NODE_HARNESS: Final = r"""
import {readFileSync} from "node:fs";
const source = readFileSync(process.argv[1], "utf8");
const boundary = source.indexOf("function initializeAdmin");
if (boundary < 0) throw new Error("admin initialization boundary missing");
const declarations = source.slice(0, boundary);
const moduleSource = `${declarations}\nexport {parseAdminDto, parseJson};`;
const encoded = Buffer.from(moduleSource).toString("base64");
const module = await import(`data:text/javascript;base64,${encoded}`);
const cases = JSON.parse(readFileSync(0, "utf8"));
const results = cases.map(({kind, payload}) => {
  const raw = kind.startsWith("raw:");
  const schemaKind = raw ? kind.slice(4) : kind;
  try { module.parseAdminDto(schemaKind, raw ? module.parseJson(payload) : payload); return true; }
  catch { return false; }
});
process.stdout.write(JSON.stringify(results));
"""

_UUID: Final = "00000000-0000-4000-8000-00000000abcd"
_TIME: Final = "2026-07-12T01:02:03.123456Z"
_UPSTREAM: Final[dict[str, JsonValue]] = {
    "id": _UUID,
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
    "created_at": _TIME,
    "updated_at": _TIME,
}
_DOWNSTREAM: Final[dict[str, JsonValue]] = {
    "id": _UUID,
    "label": "operator-token",
    "scopes": ["models:read", "chat:write"],
    "revoked_at": None,
    "request_count": 0,
    "last_used_at": None,
    "created_at": _TIME,
}
_EVENT: Final[dict[str, JsonValue]] = {
    "id": _UUID,
    "request_id": "opaque-request",
    "event_type": "upstream_attempt",
    "upstream_key_id": _UUID,
    "downstream_token_id": None,
    "outcome_class": "started",
    "status_class": None,
    "latency_ms": None,
    "occurred_at": _TIME,
}
_DASHBOARD_EVENT: Final[dict[str, JsonValue]] = {
    **_EVENT,
    "upstream_key_fingerprint": f"sha256:{'a' * 64}",
    "attempt_started_event_id": None,
}
_UPSTREAM_COUNTS: Final[dict[str, JsonValue]] = {
    "total": 2,
    "enabled": 2,
    "eligible": 1,
    "cooling": 1,
    "degraded": 1,
}
_DOWNSTREAM_COUNTS: Final[dict[str, JsonValue]] = {"total": 3, "active": 2, "revoked": 1}
_OVERVIEW: Final[dict[str, JsonValue]] = {
    "status": "ok",
    "ready": True,
    "upstream_keys": _UPSTREAM_COUNTS,
    "downstream_tokens": _DOWNSTREAM_COUNTS,
    "request_count": 42,
    "last_event_at": _TIME,
    "generated_at": _TIME,
}
_DASHBOARD_OVERVIEW: Final[dict[str, JsonValue]] = {
    **_OVERVIEW,
    "status": "degraded",
    "ready": False,
    "upstream_keys": {
        "total": 1,
        "enabled": 0,
        "eligible": 0,
        "cooling": 0,
        "degraded": 0,
    },
    "downstream_tokens": {"total": 1, "active": 1, "revoked": 0},
}
_LEDGER: Final[dict[str, JsonValue]] = {
    "status": "ok",
    "capacity_blocker": "none",
    "event_rows": 1,
    "reserved_terminal_slots": 0,
    "event_capacity": 100,
    "attempt_rows": 0,
    "attempt_capacity": 50,
    "last_maintenance_completed_at": _TIME,
    "last_pruned_event_rows": 0,
    "last_pruned_attempt_rows": 0,
    "oldest_event_at": _TIME,
}
_DASHBOARD: Final[dict[str, JsonValue]] = {
    "runtime_state": "operational",
    "readiness_cause": "no_eligible_upstream",
    "ledger": _LEDGER,
    "overview": _DASHBOARD_OVERVIEW,
    "upstream_keys": {"items": [_UPSTREAM]},
    "downstream_tokens": {"items": [_DOWNSTREAM]},
    "events": {"items": [_DASHBOARD_EVENT]},
}
_PROBE: Final[dict[str, JsonValue]] = {
    "id": _UUID,
    "enabled": False,
    "probe_status": "valid",
    "observed_at": _TIME,
}
_ISSUED: Final[dict[str, JsonValue]] = {
    **_DOWNSTREAM,
    "token": f"nblb_ds_{'b' * 64}",
}
_ERROR_DETAIL: Final[dict[str, JsonValue]] = {
    "code": "database_unavailable",
    "message": "database unavailable",
    "request_id": "opaque",
}
_ERROR: Final[dict[str, JsonValue]] = {"error": _ERROR_DETAIL}
_VALIDATION_DETAIL: Final[dict[str, JsonValue]] = {
    "code": "invalid_request",
    "message": "request validation failed",
    "request_id": "opaque",
}
_VALIDATION_ERROR: Final[dict[str, JsonValue]] = {"error": _VALIDATION_DETAIL}


def _accepted(cases: Sequence[tuple[str, JsonValue]]) -> list[bool]:
    completed = subprocess.run(  # noqa: S603
        [NODE_EXECUTABLE, "--input-type=module", "--eval", _NODE_HARNESS, str(_ADMIN_SCRIPT)],
        check=True,
        capture_output=True,
        input=json.dumps([{"kind": kind, "payload": payload} for kind, payload in cases]),
        text=True,
    )
    return _BOOL_LIST.validate_json(completed.stdout)


ADMIN_SCRIPT: Final = _ADMIN_SCRIPT
UPSTREAM_DTO: Final = _UPSTREAM
DOWNSTREAM_DTO: Final = _DOWNSTREAM
EVENT_DTO: Final = _EVENT
DASHBOARD_DTO: Final = _DASHBOARD
DASHBOARD_EVENT_DTO: Final = _DASHBOARD_EVENT
LEDGER_DTO: Final = _LEDGER
OVERVIEW_DTO: Final = _OVERVIEW
PROBE_DTO: Final = _PROBE
ERROR_DETAIL_DTO: Final = _ERROR_DETAIL


def accepted_cases(cases: Sequence[tuple[str, JsonValue]]) -> list[bool]:
    return _accepted(cases)


def _field_variants(
    payload: dict[str, JsonValue], field: str, wrong_value: JsonValue
) -> tuple[dict[str, JsonValue], ...]:
    missing = dict(payload)
    del missing[field]
    return missing, {**payload, "unexpected": "forbidden"}, {**payload, field: wrong_value}


def test_admin_script_accepts_only_exact_success_and_error_dtos() -> None:
    # Given: one canonical payload for every administration response projection.
    cases: list[tuple[str, JsonValue]] = [
        ("dashboard", _DASHBOARD),
        ("overview", _OVERVIEW),
        ("upstream", _UPSTREAM),
        ("upstreamList", {"items": [_UPSTREAM]}),
        ("downstreamList", {"items": [_DOWNSTREAM]}),
        ("eventList", {"items": [_EVENT]}),
        ("probe", _PROBE),
        ("issued", _ISSUED),
        ("error", _ERROR),
        ("validationError", _VALIDATION_ERROR),
    ]

    # When: the production JavaScript boundary parses every payload.
    accepted = _accepted(cases)

    # Then: every exact payload crosses the boundary unchanged.
    assert accepted == [True] * len(cases)


def test_admin_script_accepts_safe_internal_error_envelope() -> None:
    # Given: the exact safe 500 envelope emitted by the server boundary.
    internal_detail: dict[str, JsonValue] = {
        **_ERROR_DETAIL,
        "code": "internal_server_error",
        "message": "internal server error",
    }
    internal_error: dict[str, JsonValue] = {"error": internal_detail}

    # When/Then: the UI preserves the canonical code and request evidence.
    assert _accepted((("error", internal_error),)) == [True]


def test_admin_script_rejects_missing_extra_and_wrong_type_fields() -> None:
    # Given: missing, extra, and wrong-type variants at every closed DTO layer.
    cases: list[tuple[str, JsonValue]] = []
    exact_shapes: tuple[tuple[str, dict[str, JsonValue], str, JsonValue], ...] = (
        ("dashboard", _DASHBOARD, "runtime_state", 7),
        ("overview", _OVERVIEW, "ready", "true"),
        ("upstream", _UPSTREAM, "enabled", 0),
        ("probe", _PROBE, "enabled", "false"),
        ("issued", _ISSUED, "token", 7),
    )
    for kind, payload, field, wrong in exact_shapes:
        cases.extend((kind, variant) for variant in _field_variants(payload, field, wrong))
    list_shapes = (
        ("upstreamList", _UPSTREAM),
        ("downstreamList", _DOWNSTREAM),
        ("eventList", _EVENT),
    )
    for kind, item in list_shapes:
        wrapper: dict[str, JsonValue] = {"items": [item]}
        cases.extend((kind, variant) for variant in _field_variants(wrapper, "items", {}))
        cases.extend(
            (kind, {"items": [variant]}) for variant in _field_variants(item, next(iter(item)), 7)
        )
    overview_shapes = (
        ("upstream_keys", _UPSTREAM_COUNTS),
        ("downstream_tokens", _DOWNSTREAM_COUNTS),
    )
    for field, nested in overview_shapes:
        cases.extend(
            ("overview", {**_OVERVIEW, field: variant})
            for variant in _field_variants(nested, next(iter(nested)), "invalid")
        )
    cases.extend(
        ("dashboard", {**_DASHBOARD, "ledger": variant})
        for variant in _field_variants(_LEDGER, "status", "healthy")
    )
    cases.extend(
        (
            "dashboard",
            {**_DASHBOARD, "events": {"items": [variant]}},
        )
        for variant in _field_variants(
            _DASHBOARD_EVENT,
            "attempt_started_event_id",
            "not-a-uuid",
        )
    )
    cases.extend(("error", variant) for variant in _field_variants(_ERROR, "error", "unsafe"))
    cases.extend(
        ("error", {"error": variant}) for variant in _field_variants(_ERROR_DETAIL, "code", 7)
    )
    cases.extend(
        ("validationError", {"error": variant})
        for variant in _field_variants(
            _VALIDATION_DETAIL,
            "message",
            7,
        )
    )

    # When: the production JavaScript boundary parses every variant.
    accepted = _accepted(cases)

    # Then: no shape drift reaches a renderer.
    assert accepted == [False] * len(cases)


def test_admin_script_rejects_noncanonical_scalar_and_collection_values() -> None:
    # Given: structurally complete DTOs with noncanonical scalar or collection values.
    event_items: list[JsonValue] = []
    event_items.extend([_EVENT] * 101)
    too_many_events: dict[str, JsonValue] = {"items": event_items}
    dashboard_event_items: list[JsonValue] = []
    dashboard_event_items.extend([_DASHBOARD_EVENT] * 101)
    too_many_dashboard_events: dict[str, JsonValue] = {"items": dashboard_event_items}
    cases: list[tuple[str, JsonValue]] = [
        ("overview", {**_OVERVIEW, "status": "healthy"}),
        ("overview", {**_OVERVIEW, "request_count": -1}),
        ("overview", {**_OVERVIEW, "request_count": 1.5}),
        ("overview", {**_OVERVIEW, "request_count": 9_007_199_254_740_992}),
        ("overview", {**_OVERVIEW, "generated_at": "2026-02-30T01:02:03Z"}),
        ("upstream", {**_UPSTREAM, "id": _UUID.upper()}),
        ("upstream", {**_UPSTREAM, "fingerprint": f"sha256:{'A' * 64}"}),
        ("upstream", {**_UPSTREAM, "health_state": "disabled"}),
        ("upstream", {**_UPSTREAM, "routing_state": "enabled"}),
        ("upstream", {**_UPSTREAM, "last_status_class": "other"}),
        ("probe", {**_PROBE, "probe_status": "healthy"}),
        ("downstreamList", {"items": [{**_DOWNSTREAM, "label": " trailing "}]}),
        ("downstreamList", {"items": [{**_DOWNSTREAM, "scopes": ["chat:write", "models:read"]}]}),
        ("downstreamList", {"items": [{**_DOWNSTREAM, "scopes": ["models:read", "models:read"]}]}),
        ("issued", {**_ISSUED, "token": f"nblb_ds_{'B' * 64}"}),
        ("eventList", {"items": [{**_EVENT, "event_type": "message"}]}),
        ("eventList", {"items": [{**_EVENT, "outcome_class": "pending"}]}),
        ("eventList", too_many_events),
        ("dashboard", {**_DASHBOARD, "runtime_state": "ready"}),
        ("dashboard", {**_DASHBOARD, "readiness_cause": "unknown"}),
        ("dashboard", {**_DASHBOARD, "ledger": {**_LEDGER, "event_rows": -1}}),
        (
            "dashboard",
            {**_DASHBOARD, "events": too_many_dashboard_events},
        ),
        ("error", {"error": {**_ERROR_DETAIL, "code": "unknown_code"}}),
        (
            "validationError",
            {"error": {**_VALIDATION_DETAIL, "message": "unsafe detail"}},
        ),
    ]

    # When: the production JavaScript boundary parses every variant.
    accepted = _accepted(cases)

    # Then: enums, scopes, identifiers, timestamps, and counters all fail closed.
    assert accepted == [False] * len(cases)


def test_admin_script_preserves_canonical_postgresql_bigint_counters() -> None:
    # Given: exact raw JSON counter lexemes at and beyond the PostgreSQL bigint boundary.
    encoded = json.dumps({**_OVERVIEW, "request_count": "COUNTER"})
    maximum = encoded.replace('"COUNTER"', "9223372036854775807")
    above_maximum = encoded.replace('"COUNTER"', "9223372036854775808")
    decimal = encoded.replace('"COUNTER"', "1.0")
    exponent = encoded.replace('"COUNTER"', "1e3")

    # When: raw JSON is parsed before the overview DTO is projected.
    accepted = _accepted(
        [
            ("raw:overview", maximum),
            ("raw:overview", above_maximum),
            ("raw:overview", decimal),
            ("raw:overview", exponent),
        ]
    )

    # Then: the exact maximum survives while overflow and noninteger lexemes are rejected.
    assert accepted == [True, False, False, False]


def test_admin_response_consumers_parse_before_rendering() -> None:
    # Given: every success and error body consumer in the production module.
    script = _ADMIN_SCRIPT.read_text()
    required_consumers = (
        "parseAdminDto(kind, payload).error",
        "const source = await response.text()",
        "problem: parseResponseProblem(path, response.status, source)",
        "value: parseAdminResponse(kind, source)",
        'api("/dashboard", {}, "dashboard", readDeadlineMs)',
        (
            'api(`/upstream-keys/${item.id}/${action}`, {method: "POST"}, '
            'action === "probe" ? "probe" : null, mutationDeadlineMs)'
        ),
        (
            'api("/upstream-keys", {method: "POST", headers: '
            '{"Content-Type": "application/json"}, body}, "upstream", mutationDeadlineMs)'
        ),
        (
            'api("/downstream-tokens", {method: "POST", headers: '
            '{"Content-Type": "application/json"}, body}, "issued", mutationDeadlineMs)'
        ),
    )

    # When: permissive JSON consumers and strict projection calls are enumerated.
    consumers_are_strict = ".json()" not in script and all(
        consumer in script for consumer in required_consumers
    )

    # Then: no response body can bypass the closed DTO boundary before rendering.
    assert consumers_are_strict
