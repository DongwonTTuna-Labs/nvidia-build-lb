import json
import subprocess
from collections.abc import Sequence
from typing import Final

import pytest
from pydantic import JsonValue, TypeAdapter

from .test_admin_dto_script import ADMIN_SCRIPT, NODE_EXECUTABLE

pytestmark = pytest.mark.ui_fake

_BOOL_LIST: Final = TypeAdapter(list[bool])
_START_ID: Final = "00000000-0000-4000-8000-000000000001"
_TERMINAL_ID: Final = "00000000-0000-4000-8000-000000000002"
_OTHER_ID: Final = "00000000-0000-4000-8000-000000000003"
_OUTSIDE_ID: Final = "00000000-0000-4000-8000-000000000004"
_EARLY: Final = "2026-07-12T01:02:03.123456Z"
_LATE: Final = "2026-07-12T01:02:04.123456Z"
_LATEST: Final = "2026-07-12T01:02:05.123456Z"

_COHERENCE_HARNESS: Final = r"""
import {readFileSync} from "node:fs";
const source = readFileSync(process.argv[1], "utf8");
const boundary = source.indexOf("function initializeAdmin");
if (boundary < 0) throw new Error("admin initialization boundary missing");
const declarations = source.slice(0, boundary);
const encoded = Buffer.from(`${declarations}\nexport {snapshotIsCoherent};`).toString("base64");
const module = await import(`data:text/javascript;base64,${encoded}`);
const cases = JSON.parse(readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(module.snapshotIsCoherent)));
"""

_LOCAL_INTENT_HARNESS: Final = r"""
import {readFileSync} from "node:fs";
const source = readFileSync(process.argv[1], "utf8");
const boundary = source.indexOf("function initializeAdmin");
if (boundary < 0) throw new Error("admin initialization boundary missing");
const declarations = source.slice(0, boundary);
const encoded = Buffer.from(
  `${declarations}\nexport {pendingProbeEvidenceMatches};`,
).toString("base64");
const module = await import(`data:text/javascript;base64,${encoded}`);
const cases = JSON.parse(readFileSync(0, "utf8"));
const results = cases.map(
  ({current, evidence}) => module.pendingProbeEvidenceMatches(current, evidence),
);
process.stdout.write(JSON.stringify(results));
"""


def _event(
    event_id: str,
    *,
    outcome: str,
    occurred_at: str,
    started_id: str | None,
    event_type: str = "upstream_attempt",
) -> dict[str, JsonValue]:
    return {
        "id": event_id,
        "event_type": event_type,
        "outcome_class": outcome,
        "occurred_at": occurred_at,
        "attempt_started_event_id": started_id,
    }


def _dashboard(events: Sequence[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    return {
        "runtime_state": "operational",
        "readiness_cause": "no_eligible_upstream",
        "ledger": {
            "status": "ok",
            "capacity_blocker": "none",
            "event_rows": len(events),
            "reserved_terminal_slots": 0,
            "event_capacity": 100,
            "attempt_rows": 0,
            "attempt_capacity": 100,
        },
        "overview": {
            "status": "degraded",
            "ready": False,
            "upstream_keys": {
                "total": 0,
                "enabled": 0,
                "eligible": 0,
                "cooling": 0,
                "degraded": 0,
            },
            "downstream_tokens": {"total": 0, "active": 0, "revoked": 0},
            "last_event_at": events[0]["occurred_at"] if events else None,
        },
        "upstream_keys": {"items": []},
        "downstream_tokens": {"items": []},
        "events": {"items": list(events)},
    }


def _coherent(cases: Sequence[dict[str, JsonValue]]) -> list[bool]:
    completed = subprocess.run(  # noqa: S603
        [
            NODE_EXECUTABLE,
            "--input-type=module",
            "--eval",
            _COHERENCE_HARNESS,
            str(ADMIN_SCRIPT),
        ],
        check=True,
        capture_output=True,
        input=json.dumps(cases),
        text=True,
    )
    return _BOOL_LIST.validate_json(completed.stdout)


def _probe_evidence_matches(cases: Sequence[dict[str, JsonValue]]) -> list[bool]:
    completed = subprocess.run(  # noqa: S603
        [
            NODE_EXECUTABLE,
            "--input-type=module",
            "--eval",
            _LOCAL_INTENT_HARNESS,
            str(ADMIN_SCRIPT),
        ],
        check=True,
        capture_output=True,
        input=json.dumps(cases),
        text=True,
    )
    return _BOOL_LIST.validate_json(completed.stdout)


def test_dashboard_attempt_pairing_uses_one_exact_started_event_id() -> None:
    start = _event(_START_ID, outcome="started", occurred_at=_EARLY, started_id=None)
    terminal = _event(
        _TERMINAL_ID,
        outcome="failed",
        occurred_at=_LATE,
        started_id=_START_ID,
    )
    duplicate = _event(
        _OTHER_ID,
        outcome="cancelled",
        occurred_at=_LATEST,
        started_id=_START_ID,
    )
    configuration_with_link = _event(
        _TERMINAL_ID,
        outcome="succeeded",
        occurred_at=_LATE,
        started_id=_START_ID,
        event_type="upstream_key_enabled",
    )
    mismatched_type = _event(
        _TERMINAL_ID,
        outcome="failed",
        occurred_at=_LATE,
        started_id=_START_ID,
        event_type="upstream_probe",
    )
    outside_window = _event(
        _TERMINAL_ID,
        outcome="failed",
        occurred_at=_LATE,
        started_id=_OUTSIDE_ID,
    )
    legacy_terminal = _event(
        _TERMINAL_ID,
        outcome="failed",
        occurred_at=_LATE,
        started_id=None,
    )

    assert _coherent(
        [
            _dashboard([]),
            _dashboard([terminal, start]),
            _dashboard([duplicate, terminal, start]),
            _dashboard([configuration_with_link, start]),
            _dashboard([mismatched_type, start]),
            _dashboard([outside_window]),
            _dashboard([legacy_terminal]),
        ]
    ) == [True, True, False, False, False, True, True]


def test_dashboard_event_order_compares_utc_instants_before_uuid_ties() -> None:
    whole = "2026-07-12T01:02:03Z"
    fraction = "2026-07-12T01:02:03.100000Z"
    later_microsecond = "2026-07-12T01:02:03.100001Z"
    lower = _event(_START_ID, outcome="failed", occurred_at=whole, started_id=None)
    fraction_event = _event(
        _TERMINAL_ID,
        outcome="failed",
        occurred_at=fraction,
        started_id=None,
    )
    later_microsecond_event = _event(
        _OTHER_ID,
        outcome="failed",
        occurred_at=later_microsecond,
        started_id=None,
    )
    same_instant_higher_id = _event(
        _TERMINAL_ID,
        outcome="failed",
        occurred_at=whole,
        started_id=None,
    )

    assert _coherent(
        [
            _dashboard([fraction_event, lower]),
            _dashboard([lower, fraction_event]),
            _dashboard([later_microsecond_event, fraction_event]),
            _dashboard([fraction_event, later_microsecond_event]),
            _dashboard([same_instant_higher_id, lower]),
            _dashboard([lower, same_instant_higher_id]),
        ]
    ) == [True, False, True, False, True, False]


def test_probe_intent_rejects_every_later_microsecond_version() -> None:
    observed = "2026-07-12T01:02:03.100000Z"

    def case(
        updated_at: str,
        *,
        enabled: bool = False,
        health: str = "healthy",
        status: str = "success",
    ) -> dict[str, JsonValue]:
        return {
            "current": {
                "enabled": enabled,
                "health_state": health,
                "last_status_class": status,
                "updated_at": updated_at,
            },
            "evidence": {"observedAt": observed},
        }

    assert _probe_evidence_matches(
        [
            case("2026-07-12T01:02:03.099999Z"),
            case(observed),
            case("2026-07-12T01:02:03.100001Z"),
            case("2026-07-12T01:02:03.100999Z"),
            case("2026-07-12T01:02:03.101000Z"),
            case("invalid"),
            case(observed, enabled=True),
            case(observed, health="degraded"),
            case(observed, status="rate_limited"),
        ]
    ) == [True, True, False, False, False, False, False, False, False]
