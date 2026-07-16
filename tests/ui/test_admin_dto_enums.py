import json
import subprocess
from collections.abc import Sequence

import pytest
from pydantic import JsonValue, TypeAdapter

from .test_admin_dto_script import (
    ADMIN_SCRIPT,
    DASHBOARD_DTO,
    DOWNSTREAM_DTO,
    ERROR_DETAIL_DTO,
    EVENT_DTO,
    LEDGER_DTO,
    NODE_EXECUTABLE,
    OVERVIEW_DTO,
    PROBE_DTO,
    UPSTREAM_DTO,
    accepted_cases,
)

pytestmark = pytest.mark.ui_fake

_STRING_MATRIX = TypeAdapter(list[list[str]])
_ORDER_HARNESS = r"""
import {readFileSync} from "node:fs";
const source = readFileSync(process.argv[1], "utf8");
const boundary = source.indexOf("function initializeAdmin");
if (boundary < 0) throw new Error("admin initialization boundary missing");
const declarations = source.slice(0, boundary);
const encoded = Buffer.from(`${declarations}\nexport {parseAdminDto};`).toString("base64");
const module = await import(`data:text/javascript;base64,${encoded}`);
const cases = JSON.parse(readFileSync(0, "utf8"));
const results = cases.map(({kind, payload}) =>
  module.parseAdminDto(kind, payload).items.map(({id}) => id)
);
process.stdout.write(JSON.stringify(results));
"""


def _ordered_ids(cases: Sequence[tuple[str, JsonValue]]) -> list[list[str]]:
    completed = subprocess.run(  # noqa: S603
        [NODE_EXECUTABLE, "--input-type=module", "--eval", _ORDER_HARNESS, str(ADMIN_SCRIPT)],
        check=True,
        capture_output=True,
        input=json.dumps([{"kind": kind, "payload": payload} for kind, payload in cases]),
        text=True,
    )
    return _STRING_MATRIX.validate_json(completed.stdout)


def _dashboard_field(field: str, value: JsonValue) -> dict[str, JsonValue]:
    return {**DASHBOARD_DTO, field: value}


def _dashboard_ledger_field(field: str, value: JsonValue) -> dict[str, JsonValue]:
    ledger: dict[str, JsonValue] = {**LEDGER_DTO, field: value}
    return {**DASHBOARD_DTO, "ledger": ledger}


def test_admin_script_accepts_every_value_in_each_closed_enum() -> None:
    # Given: every positive value from every administration response enum.
    health_states = ("unknown", "healthy", "degraded")
    routing_states = ("disabled", "eligible", "cooldown", "quarantined")
    status_classes = (
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
    )
    probe_statuses = ("valid", "invalid_credential", "rate_limited", "upstream_unavailable")
    event_types = (
        "upstream_key_created",
        "upstream_key_enabled",
        "upstream_key_disabled",
        "upstream_key_deleted",
        "upstream_probe",
        "downstream_token_issued",
        "downstream_token_revoked",
        "upstream_attempt",
    )
    outcomes = ("started", "succeeded", "failed", "cancelled")
    safe_codes = (
        "host_forbidden",
        "origin_forbidden",
        "unauthorized",
        "admin_unauthorized",
        "insufficient_scope",
        "model_not_found",
        "resource_not_found",
        "resource_conflict",
        "invalid_request",
        "no_upstream_keys",
        "database_unavailable",
        "upstream_auth_error",
        "upstream_credits_exhausted",
        "upstream_timeout",
        "upstream_rate_limited",
        "upstream_request_rejected",
        "upstream_internal_error",
        "upstream_bad_gateway",
        "upstream_unavailable",
        "upstream_protocol_error",
        "poll_timeout",
        "admin_read_timeout",
        "admin_mutation_timeout",
        "admin_mutation_response_invalid",
        "admin_mutation_settling",
        "runtime_unavailable",
        "ledger_capacity_exhausted",
    )
    cases: list[tuple[str, JsonValue]] = []
    cases.extend(("upstream", {**UPSTREAM_DTO, "health_state": value}) for value in health_states)
    cases.extend(("upstream", {**UPSTREAM_DTO, "routing_state": value}) for value in routing_states)
    cases.extend(
        ("upstream", {**UPSTREAM_DTO, "last_status_class": value}) for value in status_classes
    )
    cases.extend(("probe", {**PROBE_DTO, "probe_status": value}) for value in probe_statuses)
    cases.extend(("overview", {**OVERVIEW_DTO, "status": value}) for value in ("ok", "degraded"))
    cases.extend(
        ("eventList", {"items": [{**EVENT_DTO, "event_type": value}]}) for value in event_types
    )
    cases.extend(
        ("eventList", {"items": [{**EVENT_DTO, "outcome_class": value}]}) for value in outcomes
    )
    cases.extend(
        ("eventList", {"items": [{**EVENT_DTO, "status_class": value}]}) for value in status_classes
    )
    cases.extend(("error", {"error": {**ERROR_DETAIL_DTO, "code": value}}) for value in safe_codes)
    cases.extend(
        ("dashboard", _dashboard_field("runtime_state", value))
        for value in ("operational", "unavailable")
    )
    cases.extend(
        ("dashboard", _dashboard_field("readiness_cause", value))
        for value in (
            "ready",
            "runtime_unavailable",
            "ledger_capacity_exhausted",
            "no_eligible_upstream",
        )
    )
    cases.extend(
        ("dashboard", _dashboard_ledger_field("status", value))
        for value in (
            "ok",
            "maintenance_overdue",
            "capacity_exhausted_recovering",
            "capacity_blocked",
        )
    )
    cases.extend(
        ("dashboard", _dashboard_ledger_field("capacity_blocker", value))
        for value in (
            "none",
            "active_attempts",
            "reconciliation_grace",
            "lock_contention",
            "orphaned_pending",
            "legacy_unlinked",
        )
    )

    # When: the production JavaScript boundary parses the complete positive matrix.
    accepted = accepted_cases(cases)

    # Then: every locked value, and no untested positive branch, is accepted.
    assert accepted == [True] * len(cases)


def test_admin_list_dtos_preserve_the_server_supplied_item_order() -> None:
    # Given: three list DTOs with deliberately non-sorted canonical IDs.
    ids = (
        "00000000-0000-4000-8000-000000000003",
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    )
    cases: list[tuple[str, JsonValue]] = [
        ("upstreamList", {"items": [{**UPSTREAM_DTO, "id": item_id} for item_id in ids]}),
        ("downstreamList", {"items": [{**DOWNSTREAM_DTO, "id": item_id} for item_id in ids]}),
        ("eventList", {"items": [{**EVENT_DTO, "id": item_id} for item_id in ids]}),
    ]

    # When: each exact DTO is parsed without a renderer or sorting helper.
    observed = _ordered_ids(cases)

    # Then: every array retains the server-provided order byte-for-byte.
    assert observed == [list(ids), list(ids), list(ids)]
