#!/usr/bin/env python3
"""Secret-safe host-owned readiness probe for current and prior app images."""

import http.client
import json
import os
import re
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum, unique
from pathlib import Path
from time import monotonic, sleep
from typing import TypeGuard
from unicodedata import category
from uuid import UUID

_HTTP_OK = 200
_HTTP_NOT_FOUND = 404
_INPUT_ERROR = 64
_ARGUMENT_COUNTS = frozenset({4, 5})
_RUNTIME_WITH_GENERATION_ARGUMENT_COUNT = 5
_ADMIN_PREFIX = "nblb_admin_"
_ADMIN_HEX_LENGTH = 64
_TOKEN_MODE = 0o600
_MAX_PORT = 65_535
_MAX_COUNTER = 9_223_372_036_854_775_807
_MAX_LABEL_SCALARS = 128
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_REQUEST_DEADLINE_SECONDS = 2.0
_LEGACY_STABILITY_SECONDS = 30.0
_CANONICAL_HOST = "127.0.0.1:2456"
_DOCKER_EXECUTABLE = "/usr/bin/docker"
_GENERATION_DEADLINE_SECONDS = 2.0
_GENERATION_FIELD_COUNT = 2
_RESPONSE_LENGTH_INVALID = "response length invalid"
_RESPONSE_TOO_LARGE = "response too large"
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TIMESTAMP_DATE = r"(?!0000)\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
_TIMESTAMP_TIME = r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?Z\Z"
_TIMESTAMP = re.compile(f"{_TIMESTAMP_DATE}{_TIMESTAMP_TIME}")
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}\Z")
_CONTAINER_STARTED_AT = re.compile(
    f"{_TIMESTAMP_DATE}" r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?Z\Z"
)

_OPERATOR_READINESS_KEYS = frozenset(
    {"runtime_state", "readiness_cause", "ledger_status", "capacity_blocker"}
)

_DASHBOARD_KEYS = frozenset(
    {
        "runtime_state",
        "readiness_cause",
        "ledger",
        "overview",
        "upstream_keys",
        "downstream_tokens",
        "events",
    }
)
_LEDGER_KEYS = frozenset(
    {
        "status",
        "capacity_blocker",
        "event_rows",
        "reserved_terminal_slots",
        "event_capacity",
        "attempt_rows",
        "attempt_capacity",
        "last_maintenance_completed_at",
        "last_pruned_event_rows",
        "last_pruned_attempt_rows",
        "oldest_event_at",
    }
)
_OVERVIEW_KEYS = frozenset(
    {
        "status",
        "ready",
        "upstream_keys",
        "downstream_tokens",
        "request_count",
        "last_event_at",
        "generated_at",
    }
)
_UPSTREAM_OVERVIEW_KEYS = frozenset({"total", "enabled", "eligible", "cooling", "degraded"})
_DOWNSTREAM_OVERVIEW_KEYS = frozenset({"total", "active", "revoked"})
_UPSTREAM_KEYS = frozenset(
    {
        "id",
        "fingerprint",
        "enabled",
        "routing_state",
        "health_state",
        "cooldown_until",
        "request_count",
        "success_count",
        "failure_count",
        "last_status_class",
        "last_used_at",
        "created_at",
        "updated_at",
    }
)
_DOWNSTREAM_KEYS = frozenset(
    {
        "label",
        "scopes",
        "id",
        "revoked_at",
        "request_count",
        "last_used_at",
        "created_at",
    }
)
_EVENT_KEYS = frozenset(
    {
        "id",
        "request_id",
        "event_type",
        "upstream_key_id",
        "downstream_token_id",
        "outcome_class",
        "status_class",
        "latency_ms",
        "occurred_at",
        "upstream_key_fingerprint",
        "attempt_started_event_id",
    }
)
_RUNTIME_STATES = frozenset({"operational", "unavailable"})
_READINESS_CAUSES = frozenset(
    {"ready", "runtime_unavailable", "ledger_capacity_exhausted", "no_eligible_upstream"}
)
_LEDGER_STATUSES = frozenset(
    {"ok", "maintenance_overdue", "capacity_exhausted_recovering", "capacity_blocked"}
)
_CAPACITY_BLOCKERS = frozenset(
    {
        "none",
        "active_attempts",
        "reconciliation_grace",
        "lock_contention",
        "orphaned_pending",
        "legacy_unlinked",
    }
)
_ROUTING_STATES = frozenset({"disabled", "eligible", "cooldown", "quarantined"})
_HEALTH_STATES = frozenset({"unknown", "healthy", "degraded"})
_STATUS_CLASSES = frozenset(
    {
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
        "cancelled",
        "delivery_failed",
    }
)
_EVENT_TYPES = frozenset(
    {
        "upstream_key_created",
        "upstream_key_enabled",
        "upstream_key_disabled",
        "upstream_key_deleted",
        "upstream_probe",
        "downstream_token_issued",
        "downstream_token_revoked",
        "upstream_attempt",
    }
)
_EVENT_OUTCOMES = frozenset({"started", "succeeded", "failed", "cancelled"})
_SCOPES = frozenset({("models:read",), ("chat:write",), ("models:read", "chat:write")})
_TRANSIENT_BLOCKERS = frozenset(
    {"none", "active_attempts", "reconciliation_grace", "lock_contention"}
)
_PERMANENT_BLOCKERS = frozenset({"orphaned_pending", "legacy_unlinked"})
_ATTEMPT_EVENT_TYPES = frozenset({"upstream_attempt", "upstream_probe"})


@unique
class OperatorProbeMode(StrEnum):
    """Closed checks exposed to local operator workflows."""

    RUNTIME = "runtime"
    LEDGER_CAPACITY = "ledger-capacity"


def _json_object(payload: bytes) -> dict[str, object] | None:
    try:
        document: object = json.loads(payload)  # pyright: ignore[reportAny]
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return None
    return document if _is_string_object(document) else None


def _is_string_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(
        isinstance(key, str)
        for key in value  # pyright: ignore[reportUnknownVariableType]
    )


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _exact_object(value: object, keys: frozenset[str]) -> dict[str, object] | None:
    if not _is_string_object(value) or frozenset(value) != keys:
        return None
    return value


def _is_enum(value: object, values: frozenset[str]) -> bool:
    return isinstance(value, str) and value in values


def _is_counter(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX_COUNTER


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _timestamp_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _is_timestamp(value: object) -> bool:
    return _timestamp_instant(value) is not None


def _is_optional(value: object, validator: Callable[[object], bool]) -> bool:
    return value is None or validator(value)


def _valid_counts(value: object, keys: frozenset[str]) -> bool:
    counts = _exact_object(value, keys)
    return counts is not None and all(_is_counter(item) for item in counts.values())


def _valid_overview(value: object) -> bool:
    overview = _exact_object(value, _OVERVIEW_KEYS)
    return (
        overview is not None
        and _is_enum(overview["status"], frozenset({"ok", "degraded"}))
        and type(overview["ready"]) is bool
        and _valid_counts(overview["upstream_keys"], _UPSTREAM_OVERVIEW_KEYS)
        and _valid_counts(overview["downstream_tokens"], _DOWNSTREAM_OVERVIEW_KEYS)
        and _is_counter(overview["request_count"])
        and _is_optional(overview["last_event_at"], _is_timestamp)
        and _is_timestamp(overview["generated_at"])
    )


def _valid_ledger(value: object) -> bool:
    ledger = _exact_object(value, _LEDGER_KEYS)
    return (
        ledger is not None
        and _is_enum(ledger["status"], _LEDGER_STATUSES)
        and _is_enum(ledger["capacity_blocker"], _CAPACITY_BLOCKERS)
        and all(
            _is_counter(ledger[key])
            for key in (
                "event_rows",
                "reserved_terminal_slots",
                "event_capacity",
                "attempt_rows",
                "attempt_capacity",
                "last_pruned_event_rows",
                "last_pruned_attempt_rows",
            )
        )
        and _is_optional(ledger["last_maintenance_completed_at"], _is_timestamp)
        and _is_optional(ledger["oldest_event_at"], _is_timestamp)
    )


def _valid_label(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_LABEL_SCALARS
        and value == value.strip()
        and not any(
            _SURROGATE_MIN <= ord(character) <= _SURROGATE_MAX or category(character) == "Cc"
            for character in value
        )
    )


def _valid_scopes(value: object) -> bool:
    if not _is_object_list(value):
        return False
    scopes: list[str] = []
    for scope in value:
        if not isinstance(scope, str):
            return False
        scopes.append(scope)
    return tuple(scopes) in _SCOPES


def _valid_upstream(value: object) -> bool:
    item = _exact_object(value, _UPSTREAM_KEYS)
    return (
        item is not None
        and _is_uuid(item["id"])
        and isinstance(item["fingerprint"], str)
        and _FINGERPRINT.fullmatch(item["fingerprint"]) is not None
        and type(item["enabled"]) is bool
        and _is_enum(item["routing_state"], _ROUTING_STATES)
        and _is_enum(item["health_state"], _HEALTH_STATES)
        and _is_optional(item["cooldown_until"], _is_timestamp)
        and all(
            _is_counter(item[key]) for key in ("request_count", "success_count", "failure_count")
        )
        and (
            item["last_status_class"] is None
            or _is_enum(item["last_status_class"], _STATUS_CLASSES)
        )
        and _is_optional(item["last_used_at"], _is_timestamp)
        and _is_timestamp(item["created_at"])
        and _is_timestamp(item["updated_at"])
    )


def _valid_downstream(value: object) -> bool:
    item = _exact_object(value, _DOWNSTREAM_KEYS)
    return (
        item is not None
        and _valid_label(item["label"])
        and _valid_scopes(item["scopes"])
        and _is_uuid(item["id"])
        and _is_optional(item["revoked_at"], _is_timestamp)
        and _is_counter(item["request_count"])
        and _is_optional(item["last_used_at"], _is_timestamp)
        and _is_timestamp(item["created_at"])
    )


def _valid_event(value: object) -> bool:
    item = _exact_object(value, _EVENT_KEYS)
    return (
        item is not None
        and _is_uuid(item["id"])
        and isinstance(item["request_id"], str)
        and bool(item["request_id"])
        and _is_enum(item["event_type"], _EVENT_TYPES)
        and _is_optional(item["upstream_key_id"], _is_uuid)
        and _is_optional(item["downstream_token_id"], _is_uuid)
        and _is_enum(item["outcome_class"], _EVENT_OUTCOMES)
        and (item["status_class"] is None or _is_enum(item["status_class"], _STATUS_CLASSES))
        and _is_optional(item["latency_ms"], _is_counter)
        and _is_timestamp(item["occurred_at"])
        and (
            item["upstream_key_fingerprint"] is None
            or (
                isinstance(item["upstream_key_fingerprint"], str)
                and _FINGERPRINT.fullmatch(item["upstream_key_fingerprint"]) is not None
            )
        )
        and _is_optional(item["attempt_started_event_id"], _is_uuid)
    )


def _valid_list(
    value: object,
    validator: Callable[[object], bool],
    *,
    maximum: int | None = None,
) -> bool:
    response = _exact_object(value, frozenset({"items"}))
    if response is None or not _is_object_list(response["items"]):
        return False
    items = response["items"]
    return (maximum is None or len(items) <= maximum) and all(validator(item) for item in items)


def _valid_dashboard(dashboard: dict[str, object]) -> bool:
    return (
        frozenset(dashboard) == _DASHBOARD_KEYS
        and _is_enum(dashboard["runtime_state"], _RUNTIME_STATES)
        and _is_enum(dashboard["readiness_cause"], _READINESS_CAUSES)
        and _valid_ledger(dashboard["ledger"])
        and _valid_overview(dashboard["overview"])
        and _valid_list(dashboard["upstream_keys"], _valid_upstream)
        and _valid_list(dashboard["downstream_tokens"], _valid_downstream)
        and _valid_list(dashboard["events"], _valid_event, maximum=100)
    )


def _exact_items(value: object, keys: frozenset[str]) -> list[dict[str, object]] | None:
    response = _exact_object(value, frozenset({"items"}))
    if response is None or not _is_object_list(response["items"]):
        return None
    items: list[dict[str, object]] = []
    for raw_item in response["items"]:
        item = _exact_object(raw_item, keys)
        if item is None:
            return None
        items.append(item)
    return items


def _counter_value(value: object) -> int | None:
    if type(value) is not int or value < 0 or value > _MAX_COUNTER:
        return None
    return value


def _ledger_is_coherent(ledger: dict[str, object]) -> bool:
    status = ledger["status"]
    blocker = ledger["capacity_blocker"]
    invalid_status = (
        not isinstance(status, str)
        or not isinstance(blocker, str)
        or (status in {"ok", "maintenance_overdue"} and blocker != "none")
        or (status == "capacity_exhausted_recovering" and blocker not in _TRANSIENT_BLOCKERS)
        or (status == "capacity_blocked" and blocker not in _PERMANENT_BLOCKERS)
    )
    if invalid_status:
        return False
    event_rows = _counter_value(ledger["event_rows"])
    reserved = _counter_value(ledger["reserved_terminal_slots"])
    event_capacity = _counter_value(ledger["event_capacity"])
    attempt_rows = _counter_value(ledger["attempt_rows"])
    attempt_capacity = _counter_value(ledger["attempt_capacity"])
    if (
        event_rows is None
        or reserved is None
        or event_capacity is None
        or attempt_rows is None
        or attempt_capacity is None
    ):
        return False
    event_available = event_rows + reserved + 2 <= event_capacity
    attempt_available = attempt_rows + 1 <= attempt_capacity
    if status == "capacity_blocked":
        return True
    return (status == "capacity_exhausted_recovering") is not (
        event_available and attempt_available
    )


def _events_are_ordered(events: list[dict[str, object]]) -> bool:
    for index in range(1, len(events)):
        previous = events[index - 1]
        current = events[index]
        previous_at = previous["occurred_at"]
        current_at = current["occurred_at"]
        previous_id = previous["id"]
        current_id = current["id"]
        previous_instant = _timestamp_instant(previous_at)
        current_instant = _timestamp_instant(current_at)
        if previous_instant is None or current_instant is None:
            return False
        try:
            previous_uuid = UUID(previous_id) if isinstance(previous_id, str) else None
            current_uuid = UUID(current_id) if isinstance(current_id, str) else None
        except ValueError:
            return False
        if previous_uuid is None or current_uuid is None:
            return False
        if previous_instant < current_instant or (
            previous_instant == current_instant and previous_uuid.int < current_uuid.int
        ):
            return False
    return True


def _event_links_are_coherent(events: list[dict[str, object]]) -> bool:
    events_by_id: dict[str, dict[str, object]] = {}
    for item in events:
        event_id = item["id"]
        if not isinstance(event_id, str):
            return False
        events_by_id[event_id] = item
    linked_start_ids: set[str] = set()
    for item in events:
        started_id = item["attempt_started_event_id"]
        outcome = item["outcome_class"]
        event_type = item["event_type"]
        invalid_owner = (outcome == "started" and started_id is not None) or (
            event_type not in _ATTEMPT_EVENT_TYPES and started_id is not None
        )
        if invalid_owner:
            return False
        if started_id is None:
            continue
        if not isinstance(started_id, str) or started_id in linked_start_ids:
            return False
        linked_start_ids.add(started_id)
        started = events_by_id.get(started_id)
        if started is not None and (
            started["outcome_class"] != "started" or started["event_type"] != event_type
        ):
            return False
    return True


def _dashboard_events_are_coherent(
    events: list[dict[str, object]],
    overview: dict[str, object],
) -> bool:
    newest = events[0]["occurred_at"] if events else None
    return (
        newest == overview["last_event_at"]
        and _events_are_ordered(events)
        and _event_links_are_coherent(events)
    )


def _dashboard_readiness_is_coherent(
    dashboard: dict[str, object],
    overview: dict[str, object],
    ledger: dict[str, object],
) -> bool:
    ready = overview["ready"]
    overview_status = overview["status"]
    cause = dashboard["readiness_cause"]
    runtime = dashboard["runtime_state"]
    ledger_status = ledger["status"]
    upstream_counts = _exact_object(overview["upstream_keys"], _UPSTREAM_OVERVIEW_KEYS)
    if (
        type(ready) is not bool
        or not all(
            isinstance(value, str) for value in (overview_status, cause, runtime, ledger_status)
        )
        or upstream_counts is None
    ):
        return False
    eligible = _counter_value(upstream_counts["eligible"])
    if eligible is None:
        return False
    capacity_blocked = ledger_status in {
        "capacity_exhausted_recovering",
        "capacity_blocked",
    }
    return (
        ready is (cause == "ready")
        and (overview_status == "ok") is ready
        and (runtime == "unavailable") is (cause == "runtime_unavailable")
        and (runtime != "operational" or capacity_blocked is (cause == "ledger_capacity_exhausted"))
        and (cause != "no_eligible_upstream" or eligible == 0)
        and (cause != "ready" or eligible > 0)
    )


def _snapshot_is_coherent(dashboard: dict[str, object]) -> bool:
    overview = _exact_object(dashboard["overview"], _OVERVIEW_KEYS)
    ledger = _exact_object(dashboard["ledger"], _LEDGER_KEYS)
    upstreams = _exact_items(dashboard["upstream_keys"], _UPSTREAM_KEYS)
    downstreams = _exact_items(dashboard["downstream_tokens"], _DOWNSTREAM_KEYS)
    events = _exact_items(dashboard["events"], _EVENT_KEYS)
    if (
        overview is None
        or ledger is None
        or upstreams is None
        or downstreams is None
        or events is None
    ):
        return False
    upstream_counts = _exact_object(overview["upstream_keys"], _UPSTREAM_OVERVIEW_KEYS)
    downstream_counts = _exact_object(overview["downstream_tokens"], _DOWNSTREAM_OVERVIEW_KEYS)
    if upstream_counts is None or downstream_counts is None:
        return False
    expected_upstream = {
        "total": len(upstreams),
        "enabled": sum(item["enabled"] is True for item in upstreams),
        "eligible": sum(item["routing_state"] == "eligible" for item in upstreams),
        "cooling": sum(item["routing_state"] == "cooldown" for item in upstreams),
        "degraded": sum(item["health_state"] == "degraded" for item in upstreams),
    }
    revoked = sum(item["revoked_at"] is not None for item in downstreams)
    expected_downstream = {
        "total": len(downstreams),
        "active": len(downstreams) - revoked,
        "revoked": revoked,
    }
    if any(upstream_counts[key] != value for key, value in expected_upstream.items()):
        return False
    if any(downstream_counts[key] != value for key, value in expected_downstream.items()):
        return False
    return (
        _ledger_is_coherent(ledger)
        and _dashboard_events_are_coherent(events, overview)
        and _dashboard_readiness_is_coherent(dashboard, overview, ledger)
    )


def _operator_readiness_is_coherent(readiness: dict[str, object]) -> bool:
    runtime = readiness["runtime_state"]
    cause = readiness["readiness_cause"]
    ledger_status = readiness["ledger_status"]
    blocker = readiness["capacity_blocker"]
    if not (
        _is_enum(runtime, _RUNTIME_STATES)
        and _is_enum(cause, _READINESS_CAUSES)
        and _is_enum(ledger_status, _LEDGER_STATUSES)
        and _is_enum(blocker, _CAPACITY_BLOCKERS)
    ):
        return False
    if (runtime == "unavailable") is not (cause == "runtime_unavailable"):
        return False
    if ledger_status in {"ok", "maintenance_overdue"}:
        ledger_is_blocked = False
        if blocker != "none":
            return False
    elif ledger_status == "capacity_exhausted_recovering":
        ledger_is_blocked = True
        if blocker not in _TRANSIENT_BLOCKERS:
            return False
    else:
        ledger_is_blocked = True
        if blocker not in _PERMANENT_BLOCKERS:
            return False
    return runtime != "operational" or ledger_is_blocked is (cause == "ledger_capacity_exhausted")


def operator_readiness_probe_confirmed(
    status: int,
    payload: bytes,
    mode: OperatorProbeMode,
) -> bool:
    """Validate one exact bounded operator-readiness response."""
    if status != _HTTP_OK:
        return False
    readiness = _json_object(payload)
    if (
        readiness is None
        or frozenset(readiness) != _OPERATOR_READINESS_KEYS
        or not _operator_readiness_is_coherent(readiness)
        or readiness["runtime_state"] != "operational"
    ):
        return False
    cause = readiness["readiness_cause"]
    if mode is OperatorProbeMode.RUNTIME:
        return cause in {"ready", "no_eligible_upstream", "ledger_capacity_exhausted"}
    return (
        cause in {"ready", "no_eligible_upstream"}
        and readiness["ledger_status"] in {"ok", "maintenance_overdue"}
        and readiness["capacity_blocker"] == "none"
    )


def _legacy_overview_is_coherent(overview: dict[str, object]) -> bool:
    ready = overview["ready"]
    status = overview["status"]
    upstream = _exact_object(overview["upstream_keys"], _UPSTREAM_OVERVIEW_KEYS)
    downstream = _exact_object(overview["downstream_tokens"], _DOWNSTREAM_OVERVIEW_KEYS)
    if (
        type(ready) is not bool
        or not isinstance(status, str)
        or upstream is None
        or downstream is None
    ):
        return False
    total = _counter_value(upstream["total"])
    enabled = _counter_value(upstream["enabled"])
    eligible = _counter_value(upstream["eligible"])
    cooling = _counter_value(upstream["cooling"])
    degraded = _counter_value(upstream["degraded"])
    token_total = _counter_value(downstream["total"])
    active = _counter_value(downstream["active"])
    revoked = _counter_value(downstream["revoked"])
    if any(
        value is None
        for value in (total, enabled, eligible, cooling, degraded, token_total, active, revoked)
    ):
        return False
    if (
        total is None
        or enabled is None
        or eligible is None
        or cooling is None
        or degraded is None
        or token_total is None
        or active is None
        or revoked is None
    ):
        return False
    return (
        ready is (status == "ok")
        and ready is (eligible > 0)
        and eligible + cooling <= enabled <= total
        and degraded <= total
        and active + revoked == token_total
    )


def dashboard_probe_confirmed(
    status: int,
    payload: bytes,
    mode: OperatorProbeMode,
) -> bool:
    """Validate one exact current dashboard response against an operator gate."""
    if status != _HTTP_OK:
        return False
    dashboard = _json_object(payload)
    if (
        dashboard is None
        or not _valid_dashboard(dashboard)
        or not _snapshot_is_coherent(dashboard)
        or dashboard["runtime_state"] != "operational"
    ):
        return False
    readiness = dashboard["readiness_cause"]
    if mode is OperatorProbeMode.RUNTIME:
        return readiness in {"ready", "no_eligible_upstream", "ledger_capacity_exhausted"}
    ledger = _exact_object(dashboard["ledger"], _LEDGER_KEYS)
    if ledger is None:
        return False
    return (
        readiness in {"ready", "no_eligible_upstream"}
        and ledger["status"] in {"ok", "maintenance_overdue"}
        and ledger["capacity_blocker"] == "none"
    )


def legacy_overview_confirmed(status: int, payload: bytes) -> bool:
    """Accept an exact overview from an image predating operator readiness."""
    if status != _HTTP_OK:
        return False
    overview = _json_object(payload)
    return not (
        overview is None
        or not _valid_overview(overview)
        or not _legacy_overview_is_coherent(overview)
    )


def container_generation(container_id: str) -> tuple[str, str] | None:
    """Return one filtered Docker generation without exposing container config."""
    if _CONTAINER_ID.fullmatch(container_id) is None:
        return None
    try:
        completed = subprocess.run(  # noqa: S603 - fixed local Docker client and arguments.
            [
                _DOCKER_EXECUTABLE,
                "inspect",
                "--format",
                "{{.Id}}\t{{.State.StartedAt}}",
                container_id,
            ],
            check=False,
            close_fds=True,
            env={"PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="ascii",
            timeout=_GENERATION_DEADLINE_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None
    fields = completed.stdout.removesuffix("\n").split("\t")
    if (
        completed.returncode != 0
        or len(fields) != _GENERATION_FIELD_COUNT
        or fields[0] != container_id
        or _CONTAINER_STARTED_AT.fullmatch(fields[1]) is None
    ):
        return None
    return fields[0], fields[1]


def legacy_runtime_confirmed(
    port: int,
    token: str,
    status: int,
    payload: bytes,
    container_id: str | None = None,
) -> bool:
    """Require one legacy degraded container generation to outlive fail-stop."""
    if not legacy_overview_confirmed(status, payload):
        return False
    overview = _json_object(payload)
    if overview is None or overview["ready"] is True:
        return overview is not None
    generation_before = None if container_id is None else container_generation(container_id)
    if generation_before is None:
        return False
    sleep(_LEGACY_STABILITY_SECONDS)
    repeated_status, repeated_payload = operator_http_request(port, token, "/admin/api/v1/overview")
    generation_after = container_generation(container_id)
    return (
        legacy_overview_confirmed(repeated_status, repeated_payload)
        and generation_after == generation_before
    )


def _read_token(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) != _TOKEN_MODE
        or metadata.st_nlink != 1
    ):
        raise ValueError
    token = path.read_text(encoding="ascii").removesuffix("\n")
    suffix = token.removeprefix(_ADMIN_PREFIX)
    if (
        not token.startswith(_ADMIN_PREFIX)
        or len(suffix) != _ADMIN_HEX_LENGTH
        or any(character not in "0123456789abcdef" for character in suffix)
    ):
        raise ValueError
    return token


def _deadline_expired(_signal_number: int, _frame: object | None) -> None:
    raise TimeoutError


def operator_http_request(port: int, token: str, path: str) -> tuple[int, bytes]:
    """Read one bounded canonical-host operator response without logging it."""
    previous_handler = signal.signal(signal.SIGALRM, _deadline_expired)
    started = monotonic()
    previous_timer = signal.setitimer(signal.ITIMER_REAL, _REQUEST_DEADLINE_SECONDS)
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=_REQUEST_DEADLINE_SECONDS,
    )
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Authorization": "Bearer " + token,
                "Host": _CANONICAL_HOST,
            },
        )
        response = connection.getresponse()
        declared_length = response.getheader("Content-Length")
        if declared_length is not None:
            try:
                declared_bytes = int(declared_length)
            except ValueError as error:
                raise ValueError(_RESPONSE_LENGTH_INVALID) from error
            if declared_bytes < 0 or declared_bytes > _MAX_RESPONSE_BYTES:
                raise ValueError(_RESPONSE_TOO_LARGE)
        payload = bytearray()
        while True:
            chunk = response.read1(min(_READ_CHUNK_BYTES, _MAX_RESPONSE_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > _MAX_RESPONSE_BYTES:
                raise ValueError(_RESPONSE_TOO_LARGE)
        return response.status, bytes(payload)
    finally:
        connection.close()
        _ = signal.setitimer(signal.ITIMER_REAL, 0)
        _ = signal.signal(signal.SIGALRM, previous_handler)
        previous_delay, previous_interval = previous_timer
        if previous_delay > 0:
            elapsed = monotonic() - started
            _ = signal.setitimer(
                signal.ITIMER_REAL,
                max(previous_delay - elapsed, 0.000001),
                previous_interval,
            )


def main() -> int:
    """Run one local probe without emitting bearer or response data."""
    if len(sys.argv) not in _ARGUMENT_COUNTS:
        return _INPUT_ERROR
    try:
        mode = OperatorProbeMode(sys.argv[1])
        port = int(sys.argv[2])
    except ValueError:
        return _INPUT_ERROR
    if port < 1 or port > _MAX_PORT:
        return _INPUT_ERROR
    container_id = sys.argv[4] if len(sys.argv) == _RUNTIME_WITH_GENERATION_ARGUMENT_COUNT else None
    if container_id is not None and (
        mode is not OperatorProbeMode.RUNTIME or _CONTAINER_ID.fullmatch(container_id) is None
    ):
        return _INPUT_ERROR
    try:
        token = _read_token(Path(sys.argv[3]))
        readiness_status, readiness_payload = operator_http_request(
            port, token, "/admin/api/v1/operator-readiness"
        )
        if operator_readiness_probe_confirmed(readiness_status, readiness_payload, mode):
            result = 0
        elif mode is not OperatorProbeMode.RUNTIME or readiness_status != _HTTP_NOT_FOUND:
            result = 1
        else:
            overview_status, overview_payload = operator_http_request(
                port, token, "/admin/api/v1/overview"
            )
            result = (
                0
                if legacy_runtime_confirmed(
                    port,
                    token,
                    overview_status,
                    overview_payload,
                    container_id,
                )
                else 1
            )
    except (OSError, UnicodeError, ValueError, http.client.HTTPException):
        return 1
    else:
        return result


if __name__ == "__main__":
    raise SystemExit(main())
