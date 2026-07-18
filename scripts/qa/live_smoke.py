#!/usr/bin/env python3
# ruff: noqa: BLE001, C901, EM101, EM102, PLR2004, S603, S607, SLF001, T201
"""Run the credential-safe live NVIDIA matrix against the deployed gateway."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Final, NoReturn
from uuid import UUID, uuid4

from scripts.ops import hermes_cutover as hc

_IMAGE_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}\Z")
_HOST: Final[str] = "127.0.0.1:2456"
_CREDENTIAL_RE: Final[re.Pattern[bytes]] = re.compile(
    rb"nvapi-[A-Za-z0-9_-]{20,}|nblb_admin_[0-9a-f]{64}|nblb_ds_[0-9a-f]{64}"
)
_SECRET_FILES: Final[tuple[Path, ...]] = (
    Path("/opt/nvidia-build-lb/secrets/admin_token"),
    Path("/opt/nvidia-build-lb/secrets/vault_master_key"),
    Path("/opt/nvidia-build-lb/secrets/db_password"),
)


@dataclass(frozen=True)
class IssuedToken:
    """One task-owned downstream token retained only in process memory."""

    token_id: str
    bearer: str


def _run(command: list[str], *, timeout: int = 180) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise hc.CutoverError(f"command_failed:{Path(command[0]).name}:{completed.returncode}")
    return completed.stdout


def _secret_material_variants() -> tuple[bytes, ...]:
    variants: set[bytes] = set()
    for path in _SECRET_FILES:
        payload = path.read_bytes()
        if len(payload) < 16:
            raise hc.CutoverError("runtime_secret_file_invalid")
        values = {payload}
        stripped = payload.rstrip(b"\r\n")
        if len(stripped) >= 16:
            values.add(stripped)
        for value in values:
            variants.update({value, value.hex().encode(), base64.b64encode(value)})
    return tuple(sorted(variants))


def _contains_secret(data: bytes, materials: tuple[bytes, ...]) -> bool:
    return _CREDENTIAL_RE.search(data) is not None or any(value in data for value in materials)


def _upstreams(settings: hc.Settings) -> list[dict[str, object]]:
    items = hc._admin(settings, "GET", "/upstream-keys").get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise hc.CutoverError("upstream_list_invalid")
    return items


def _token_item(settings: hc.Settings, token_id: str) -> dict[str, object]:
    return hc._token_item(settings, token_id)


def _downstream_items(settings: hc.Settings) -> list[dict[str, object]]:
    items = hc._admin(settings, "GET", "/downstream-tokens").get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise hc.CutoverError("downstream_list_invalid")
    return items


def _reconcile_issued_label(settings: hc.Settings, label: str, failure: str) -> NoReturn:
    matches = [item for item in _downstream_items(settings) if item.get("label") == label]
    if len(matches) == 1:
        token_id = matches[0].get("id")
        if not isinstance(token_id, str):
            raise hc.CutoverError("downstream_issue_reconciliation_invalid")
        try:
            UUID(token_id)
        except ValueError:
            raise hc.CutoverError("downstream_issue_reconciliation_invalid") from None
        hc._revoke_and_verify(settings, token_id)
        raise hc.CutoverError(f"{failure}_reconciled_and_revoked")
    if matches:
        raise hc.CutoverError("downstream_issue_reconciliation_ambiguous")
    raise hc.CutoverError(failure)


def _validated_issued_response(
    settings: hc.Settings,
    response: dict[str, object],
    label: str,
    scopes: list[str],
) -> IssuedToken:
    token_id = response.get("id")
    bearer = response.get("token")
    if not isinstance(token_id, str) or not isinstance(bearer, str):
        raise hc.CutoverError("downstream_issue_response_invalid")
    _ = UUID(token_id)
    if not hc._DOWNSTREAM_RE.fullmatch(bearer):
        raise hc.CutoverError("downstream_issue_token_invalid")
    item = _token_item(settings, token_id)
    if (
        item.get("label") != label
        or item.get("scopes") != scopes
        or item.get("revoked_at") is not None
    ):
        raise hc.CutoverError("downstream_issue_persisted_state_invalid")
    return IssuedToken(token_id, bearer)


def _issue(
    settings: hc.Settings,
    label: str,
    scopes: list[str],
) -> IssuedToken:
    try:
        response = hc._admin(
            settings,
            "POST",
            "/downstream-tokens",
            payload={"label": label, "scopes": scopes},
            expected=(201,),
        )
    except (OSError, ValueError, urllib.error.URLError, hc.CutoverError):
        _reconcile_issued_label(settings, label, "downstream_issue_failed")
    try:
        return _validated_issued_response(settings, response, label, scopes)
    except (OSError, ValueError, urllib.error.URLError, hc.CutoverError):
        _reconcile_issued_label(settings, label, "downstream_issue_validation_failed")


def _revoke(settings: hc.Settings, token: IssuedToken) -> None:
    hc._revoke_and_verify(settings, token.token_id)


def _probe_enable(settings: hc.Settings, key_id: str) -> None:
    probe = hc._admin(settings, "POST", f"/upstream-keys/{key_id}/probe")
    if probe.get("probe_status") != "valid":
        raise hc.CutoverError("upstream_probe_failed")
    hc._admin_post(settings, f"/upstream-keys/{key_id}/enable")


def _disable(settings: hc.Settings, key_id: str) -> None:
    hc._admin_post(settings, f"/upstream-keys/{key_id}/disable")


def _counts(settings: hc.Settings) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in _upstreams(settings):
        key_id = item.get("id")
        count = item.get("request_count")
        if not isinstance(key_id, str) or not isinstance(count, int):
            raise hc.CutoverError("upstream_counter_invalid")
        result[key_id] = count
    return result


def _models(settings: hc.Settings, bearer: str, *, expected: tuple[int, ...] = (200,)) -> None:
    media, body = hc._request(
        "GET",
        f"{settings.lb_base_url}/v1/models",
        bearer,
        expected=expected,
        timeout=30,
        host=_HOST,
    )
    if expected == (200,):
        document = hc._json_body(body)
        data = document.get("data")
        if media != "application/json" or not isinstance(data, list) or not data:
            raise hc.CutoverError("models_contract_failed")


def _stream_content(body: bytes) -> str:
    terminator = b"data: [DONE]\n\n"
    if body.count(terminator) != 1 or not body.endswith(terminator):
        raise hc.CutoverError("live_stream_contract_failed")
    content: list[str] = []
    data_frames = 0
    for line in body.splitlines():
        if not line.startswith(b"data: ") or line == b"data: [DONE]":
            continue
        data_frames += 1
        document = hc._json_body(line[6:])
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            raise hc.CutoverError("live_stream_frame_invalid")
        first = choices[0]
        delta = first.get("delta") if isinstance(first, dict) else None
        value = delta.get("content") if isinstance(delta, dict) else None
        if value is not None:
            if not isinstance(value, str):
                raise hc.CutoverError("live_stream_frame_invalid")
            content.append(value)
    rendered = "".join(content)
    if data_frames == 0 or "라이브 스모크 성공" not in rendered:
        raise hc.CutoverError("live_stream_content_failed")
    return rendered


def _chat(
    settings: hc.Settings,
    bearer: str,
    *,
    stream: bool,
    expected: tuple[int, ...] = (200,),
) -> None:
    media, body = hc._request(
        "POST",
        f"{settings.lb_base_url}/v1/chat/completions",
        bearer,
        payload={
            "model": "z-ai/glm-5.2",
            "messages": [
                {
                    "role": "user",
                    "content": "도구 없이 한국어로 정확히 '라이브 스모크 성공'이라고만 답하세요.",
                }
            ],
            "stream": stream,
            "max_tokens": 32,
        },
        expected=expected,
        timeout=360,
        host=_HOST,
    )
    if expected != (200,):
        return
    if stream:
        if media != "text/event-stream":
            raise hc.CutoverError("live_stream_contract_failed")
        _ = _stream_content(body)
        return
    document = hc._json_body(body)
    choices = document.get("choices")
    if media != "application/json" or not isinstance(choices, list) or not choices:
        raise hc.CutoverError("live_nonstream_contract_failed")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or "라이브 스모크 성공" not in content:
        raise hc.CutoverError("live_nonstream_content_failed")


def _set_all_enabled(settings: hc.Settings, key_ids: list[str]) -> None:
    for key_id in key_ids:
        _probe_enable(settings, key_id)


def _per_key_calls(settings: hc.Settings, bearer: str, key_ids: list[str]) -> dict[str, bool]:
    results: dict[str, bool] = {}
    for target in key_ids:
        for key_id in key_ids:
            if key_id != target:
                _disable(settings, key_id)
        _probe_enable(settings, target)
        before = _counts(settings)
        _chat(settings, bearer, stream=False)
        after = _counts(settings)
        if after[target] - before[target] < 1:
            raise hc.CutoverError("single_key_call_not_attributed")
        if any(after[key_id] != before[key_id] for key_id in key_ids if key_id != target):
            raise hc.CutoverError("disabled_key_received_request")
        results[target] = True
        _set_all_enabled(settings, key_ids)
    return results


def _scope_and_revoke(
    settings: hc.Settings,
    issued: list[IssuedToken],
    prefix: str,
) -> dict[str, bool]:
    models_only = _issue(settings, f"{prefix}models", ["models:read"])
    issued.append(models_only)
    _models(settings, models_only.bearer)
    _chat(settings, models_only.bearer, stream=False, expected=(403,))

    chat_only = _issue(settings, f"{prefix}chat", ["chat:write"])
    issued.append(chat_only)
    _models(settings, chat_only.bearer, expected=(403,))
    _chat(settings, chat_only.bearer, stream=False)

    revoked = _issue(settings, f"{prefix}revoked", ["models:read", "chat:write"])
    issued.append(revoked)
    _revoke(settings, revoked)
    _models(settings, revoked.bearer, expected=(401,))
    return {"models_scope": True, "chat_scope": True, "revoked_rejected": True}


def _db_sql(sql: str, *variables: str) -> str:
    command = [
        "docker",
        "exec",
        "--user",
        "70",
        "nvidia-build-lb-db-1",
        "psql",
        "--no-psqlrc",
        "--set",
        "ON_ERROR_STOP=1",
        "--username",
        "nvidia_build_lb",
        "--dbname",
        "nvidia_build_lb",
        "--tuples-only",
        "--no-align",
        "--quiet",
    ]
    for index, value in enumerate(variables, start=1):
        command.extend(["-v", f"v{index}={value}"])
    command.extend(["-c", sql])
    return _run(command).strip()


def _attempt_marker() -> str:
    marker = _db_sql("select clock_timestamp()::text")
    if not marker:
        raise hc.CutoverError("attempt_marker_invalid")
    return marker


def _round_robin_sequence(
    settings: hc.Settings,
    bearer: str,
    key_ids: list[str],
) -> dict[str, object]:
    if len(key_ids) != 2:
        raise hc.CutoverError("round_robin_requires_two_keys")
    marker_timestamp = _attempt_marker()
    before = _counts(settings)
    for _ in range(6):
        _chat(settings, bearer, stream=False)
    after = _counts(settings)
    sequence_text = _db_sql(
        """
        select upstream_key_id::text
          from upstream_attempt_receipts
         where started_at >= :'v1'::timestamptz
           and explicit_probe_key_id is null
           and terminal_outcome = 'succeeded'
         order by started_at, started_event_id;
        """,
        marker_timestamp,
    )
    sequence = sequence_text.splitlines() if sequence_text else []
    deltas = {key_id: after[key_id] - before[key_id] for key_id in key_ids}
    counts = Counter(sequence)
    alternating = all(left != right for left, right in pairwise(sequence))
    if (
        len(sequence) != 6
        or set(sequence) != set(key_ids)
        or any(counts[key_id] != 3 for key_id in key_ids)
        or sum(deltas.values()) != 6
        or any(deltas[key_id] != 3 for key_id in key_ids)
        or not alternating
    ):
        raise hc.CutoverError("round_robin_sequence_failed")
    return {
        "request_count": len(sequence),
        "per_key_counts": dict(counts),
        "counter_deltas": deltas,
        "alternating": True,
        "sequence": sequence,
    }


def _inject_key(settings: hc.Settings) -> str:
    opaque = f"invalid-live-smoke-{secrets.token_urlsafe(32)}"
    response = hc._admin(
        settings,
        "POST",
        "/upstream-keys",
        payload={"key": opaque},
        expected=(201,),
    )
    key_id = response.get("id")
    if not isinstance(key_id, str):
        raise hc.CutoverError("synthetic_key_id_invalid")
    UUID(key_id)
    return key_id


def _restore_cursor(prior_cursor: str) -> None:
    if prior_cursor:
        _ = _db_sql(
            (
                "update scheduler_state set cursor_key_id=:'v1'::uuid, "
                "updated_at=now() where singleton_id=1"
            ),
            prior_cursor,
        )
    else:
        _ = _db_sql(
            "update scheduler_state set cursor_key_id=null, updated_at=now() where singleton_id=1"
        )
    restored_cursor = _db_sql(
        "select coalesce(cursor_key_id::text,'') from scheduler_state where singleton_id=1"
    )
    if restored_cursor != prior_cursor:
        raise hc.CutoverError("cursor_restore_mismatch")


def _cleanup_controlled_failure(
    settings: hc.Settings,
    synthetic_id: str | None,
    prior_cursor: str | None,
) -> None:
    cleanup_failures: list[str] = []
    if synthetic_id is not None:
        try:
            _disable(settings, synthetic_id)
        except Exception:
            cleanup_failures.append("synthetic_disable")
        try:
            _ = hc._admin(
                settings,
                "DELETE",
                f"/upstream-keys/{synthetic_id}",
                expected=(204,),
            )
        except Exception:
            cleanup_failures.append("synthetic_delete_response")
        try:
            synthetic_absent = all(item.get("id") != synthetic_id for item in _upstreams(settings))
        except Exception:
            synthetic_absent = False
        if synthetic_absent:
            cleanup_failures = [
                failure
                for failure in cleanup_failures
                if failure not in {"synthetic_disable", "synthetic_delete_response"}
            ]
        else:
            cleanup_failures.append("synthetic_row_present")
    if prior_cursor is not None:
        try:
            _restore_cursor(prior_cursor)
        except Exception:
            cleanup_failures.append("cursor_restore")
    if cleanup_failures:
        raise hc.CutoverError("controlled_failure_cleanup_failed")


def _controlled_failure(
    settings: hc.Settings,
    bearer: str,
    real_key_ids: list[str],
) -> dict[str, object]:
    synthetic_id: str | None = None
    prior_cursor: str | None = None
    try:
        synthetic_id = _inject_key(settings)
        prior_cursor = _db_sql(
            "select coalesce(cursor_key_id::text,'') from scheduler_state where singleton_id=1"
        )
        changed = _db_sql(
            """
            update upstream_keys
               set enabled=true,
                   health_state='healthy',
                   cooldown_until=null,
                   cooldown_kind=null,
                   quarantined=false,
                   last_status_class=null,
                   updated_at=now()
             where id=:'v1'::uuid;
            update scheduler_state
               set cursor_key_id=(
                     select id from upstream_keys
                      where (created_at,id) < (
                            select created_at,id from upstream_keys where id=:'v1'::uuid
                      )
                      order by created_at desc,id desc limit 1
                   ),
                   updated_at=now()
             where singleton_id=1;
            select count(*) from upstream_keys where id=:'v1'::uuid and enabled;
            """,
            synthetic_id,
        )
        if changed.splitlines()[-1:] != ["1"]:
            raise hc.CutoverError("synthetic_key_injection_failed")
        before = _counts(settings)
        _chat(settings, bearer, stream=False)
        after = _counts(settings)
        items = {str(item["id"]): item for item in _upstreams(settings)}
        synthetic = items.get(synthetic_id)
        if synthetic is None:
            raise hc.CutoverError("synthetic_key_disappeared")
        if (
            after[synthetic_id] - before[synthetic_id] != 1
            or synthetic.get("last_status_class") != "invalid_credential"
            or synthetic.get("routing_state") != "quarantined"
        ):
            raise hc.CutoverError("synthetic_rejection_not_observed")
        alternates = [key_id for key_id in real_key_ids if after[key_id] - before[key_id] >= 1]
        if len(alternates) != 1:
            raise hc.CutoverError("controlled_failover_not_observed")
        return {
            "synthetic_key_id": synthetic_id,
            "rejection": "invalid_credential",
            "alternate_key_id": alternates[0],
            "alternate_succeeded": True,
        }
    finally:
        _cleanup_controlled_failure(settings, synthetic_id, prior_cursor)


def _controlled_cooldown(
    settings: hc.Settings,
    bearer: str,
    real_key_ids: list[str],
) -> dict[str, object]:
    synthetic_id: str | None = None
    prior_cursor: str | None = None
    try:
        synthetic_id = _inject_key(settings)
        prior_cursor = _db_sql(
            "select coalesce(cursor_key_id::text,'') from scheduler_state where singleton_id=1"
        )
        changed = _db_sql(
            """
            update upstream_keys
               set enabled=true,
                   health_state='healthy',
                   cooldown_until=now() + interval '10 minutes',
                   cooldown_kind='rate_limited',
                   quarantined=false,
                   last_status_class='rate_limited',
                   updated_at=now()
             where id=:'v1'::uuid;
            update scheduler_state
               set cursor_key_id=(
                     select id from upstream_keys
                      where (created_at,id) < (
                            select created_at,id from upstream_keys where id=:'v1'::uuid
                      )
                      order by created_at desc,id desc limit 1
                   ),
                   updated_at=now()
             where singleton_id=1;
            select count(*) from upstream_keys
             where id=:'v1'::uuid and enabled and cooldown_until > now();
            """,
            synthetic_id,
        )
        if changed.splitlines()[-1:] != ["1"]:
            raise hc.CutoverError("synthetic_cooldown_injection_failed")
        items_before = {str(item["id"]): item for item in _upstreams(settings)}
        cooled_before = items_before.get(synthetic_id)
        if (
            cooled_before is None
            or cooled_before.get("routing_state") != "cooldown"
            or cooled_before.get("cooldown_until") is None
        ):
            raise hc.CutoverError("synthetic_cooldown_not_observed")
        before = _counts(settings)
        _chat(settings, bearer, stream=False)
        after = _counts(settings)
        items_after = {str(item["id"]): item for item in _upstreams(settings)}
        cooled_after = items_after.get(synthetic_id)
        if (
            after[synthetic_id] != before[synthetic_id]
            or cooled_after is None
            or cooled_after.get("routing_state") != "cooldown"
            or cooled_after.get("cooldown_until") is None
        ):
            raise hc.CutoverError("cooled_key_received_request")
        alternates = [key_id for key_id in real_key_ids if after[key_id] - before[key_id] >= 1]
        if len(alternates) != 1:
            raise hc.CutoverError("cooldown_failover_not_observed")
        return {
            "synthetic_key_id": synthetic_id,
            "cooldown_kind": "rate_limited",
            "cooled_attempt_delta": 0,
            "alternate_key_id": alternates[0],
            "alternate_succeeded": True,
            "cooldown_preserved": True,
        }
    finally:
        _cleanup_controlled_failure(settings, synthetic_id, prior_cursor)


_PERSISTED_UPSTREAM_FIELDS: Final[tuple[str, ...]] = (
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
)


def _safe_upstream_projection(
    items: list[dict[str, object]],
    key_ids: list[str],
) -> dict[str, tuple[object, ...]]:
    projection: dict[str, tuple[object, ...]] = {}
    for item in items:
        key_id = item.get("id")
        if isinstance(key_id, str) and key_id in key_ids:
            projection[key_id] = tuple(item.get(field) for field in _PERSISTED_UPSTREAM_FIELDS)
    if set(projection) != set(key_ids):
        raise hc.CutoverError("upstream_projection_incomplete")
    return projection


def _scheduler_cursor() -> str:
    cursor = _db_sql(
        "select coalesce(cursor_key_id::text,'') from scheduler_state where singleton_id=1"
    )
    if cursor:
        try:
            UUID(cursor)
        except ValueError:
            raise hc.CutoverError("scheduler_cursor_invalid") from None
    return cursor


def _wait_app_health(settings: hc.Settings) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed loopback health URL.
                f"{settings.lb_base_url}/health",
                timeout=2,
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(1)
    raise hc.CutoverError("app_restart_health_timeout")


def _recreate_app(settings: hc.Settings, expected_ref: str) -> None:
    _ = _run(
        [
            "scripts/ops/production-compose.sh",
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "app",
        ],
        timeout=240,
    )
    _wait_app_health(settings)
    running_ref = _run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", "nvidia-build-lb-app-1"]
    ).strip()
    if running_ref != expected_ref:
        raise hc.CutoverError("app_restart_digest_changed")


def _restart_persistence(  # noqa: PLR0913
    settings: hc.Settings,
    expected_ref: str,
    all_key_ids: list[str],
    routing_key_ids: list[str],
    bearer: str,
    log_since: str,
) -> dict[str, object]:
    protected_names = ("nvidia-build-lb-db-1", "codex-lb", "agent-hermes")
    protected_before = {
        name: _run(["docker", "inspect", "--format", "{{.Id}}", name]).strip()
        for name in protected_names
    }
    before = _safe_upstream_projection(_upstreams(settings), all_key_ids)
    cursor_before = _scheduler_cursor()
    old_container_id = _run(
        ["docker", "inspect", "--format", "{{.Id}}", "nvidia-build-lb-app-1"]
    ).strip()
    old_logs = _container_logs(old_container_id, log_since)
    materials = _secret_material_variants()
    if _contains_secret(old_logs, materials):
        raise hc.CutoverError("credential_shape_in_old_app_logs")
    try:
        _recreate_app(settings, expected_ref)
    except Exception as restart_error:
        try:
            _recreate_app(settings, expected_ref)
        except Exception:
            raise hc.CutoverError("app_restart_recovery_failed") from restart_error
        raise
    running_ref = _run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", "nvidia-build-lb-app-1"]
    ).strip()
    if running_ref != expected_ref:
        raise hc.CutoverError("app_restart_digest_changed")
    new_container_id = _run(
        ["docker", "inspect", "--format", "{{.Id}}", "nvidia-build-lb-app-1"]
    ).strip()
    if not new_container_id or new_container_id == old_container_id:
        raise hc.CutoverError("app_restart_identity_unchanged")
    protected_after = {
        name: _run(["docker", "inspect", "--format", "{{.Id}}", name]).strip()
        for name in protected_names
    }
    if protected_after != protected_before:
        raise hc.CutoverError("unrelated_container_identity_changed")
    after = _safe_upstream_projection(_upstreams(settings), all_key_ids)
    cursor_after = _scheduler_cursor()
    if before != after or cursor_before != cursor_after:
        raise hc.CutoverError("upstream_state_not_persistent")
    per_key = _per_key_calls(settings, bearer, routing_key_ids)
    new_logs = _container_logs(new_container_id, log_since)
    if _contains_secret(new_logs, materials):
        raise hc.CutoverError("credential_shape_in_new_app_logs")
    return {
        "same_image": True,
        "key_state_persistent": True,
        "scheduler_cursor_persistent": True,
        "container_recreated": True,
        "old_logs_scanned": True,
        "new_logs_scanned": True,
        "unrelated_containers_unchanged": True,
        "post_restart_per_key_success": per_key,
    }


def _container_logs(container: str, since: str) -> bytes:
    completed = subprocess.run(
        ["docker", "logs", "--since", since, container],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise hc.CutoverError("container_log_read_failed")
    return completed.stdout + completed.stderr


def _runtime_metadata_scan(materials: tuple[bytes, ...]) -> dict[str, bool]:
    for container in ("nvidia-build-lb-app-1", "nvidia-build-lb-db-1"):
        metadata = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .Config.Entrypoint}}\n{{json .Config.Cmd}}\n{{json .Config.Env}}",
                container,
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if metadata.returncode != 0:
            raise hc.CutoverError("container_runtime_metadata_read_failed")
        if _contains_secret(metadata.stdout, materials) or _contains_secret(
            metadata.stderr,
            materials,
        ):
            raise hc.CutoverError("credential_shape_in_container_runtime_metadata")
    return {"filtered_args": True, "filtered_environment": True}


def _secret_safe_runtime_scan(log_since: str) -> dict[str, bool]:
    materials = _secret_material_variants()
    for container in ("nvidia-build-lb-app-1", "nvidia-build-lb-db-1"):
        if _contains_secret(_container_logs(container, log_since), materials):
            raise hc.CutoverError("credential_shape_in_runtime_logs")
    plaintext_rows = _db_sql(
        "select count(*) from upstream_keys where position(convert_to('nvapi-','UTF8') in "
        "vault_ciphertext) > 0"
    )
    if plaintext_rows != "0":
        raise hc.CutoverError("upstream_plaintext_shape_in_ciphertext")
    return {
        "logs": True,
        "database_plaintext_shape_absent": True,
        **_runtime_metadata_scan(materials),
    }


def _upstream_enabled_projection(items: list[dict[str, object]]) -> dict[str, bool]:
    projection: dict[str, bool] = {}
    for item in items:
        key_id = item.get("id")
        enabled = item.get("enabled")
        if not isinstance(key_id, str) or not isinstance(enabled, bool):
            raise hc.CutoverError("upstream_item_invalid")
        projection[key_id] = enabled
    return projection


def _validated_task_token_id(value: object) -> str:
    if not isinstance(value, str):
        raise hc.CutoverError("task_token_id_invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise hc.CutoverError("task_token_id_invalid") from None
    if str(parsed) != value:
        raise hc.CutoverError("task_token_id_invalid")
    return value


def _cleanup_matrix(  # noqa: PLR0912, PLR0913
    settings: hc.Settings,
    issued: list[IssuedToken],
    key_ids: list[str],
    initial_enabled: dict[str, bool],
    synthetic_id: str | None,
    task_label_prefix: str,
) -> dict[str, object]:
    cleanup_failures: list[str] = []
    for token in reversed(issued):
        try:
            _revoke(settings, token)
        except Exception:
            cleanup_failures.append("task_token_revoke_failed")
    try:
        task_items = [
            item
            for item in _downstream_items(settings)
            if isinstance(item.get("label"), str)
            and str(item["label"]).startswith(task_label_prefix)
        ]
        for item in task_items:
            token_id = _validated_task_token_id(item.get("id"))
            if item.get("revoked_at") is None:
                hc._revoke_and_verify(settings, token_id)
        active_task_tokens = sum(
            1
            for item in _downstream_items(settings)
            if isinstance(item.get("label"), str)
            and str(item["label"]).startswith(task_label_prefix)
            and item.get("revoked_at") is None
        )
    except Exception:
        cleanup_failures.append("task_token_reconciliation_failed")
        active_task_tokens = -1
    if active_task_tokens == 0:
        cleanup_failures = [
            failure for failure in cleanup_failures if failure != "task_token_revoke_failed"
        ]
    else:
        cleanup_failures.append("task_token_active")
    enable_restore_succeeded = True
    try:
        _set_all_enabled(settings, key_ids)
    except Exception:
        enable_restore_succeeded = False
    try:
        final_items = _upstreams(settings)
        final_enabled = _upstream_enabled_projection(final_items)
    except Exception:
        cleanup_failures.append("upstream_final_state_unknown")
        final_items = []
        final_enabled = {}
    if len(final_items) != 2 or set(final_enabled) != set(initial_enabled):
        cleanup_failures.append("upstream_row_set_restore")
    if final_enabled != initial_enabled:
        cleanup_failures.append(
            "upstream_enabled_state_restore"
            if enable_restore_succeeded
            else "upstream_enable_restore_failed"
        )
    if synthetic_id is not None and synthetic_id in final_enabled:
        cleanup_failures.append("synthetic_row_present")
    if cleanup_failures:
        raise hc.CutoverError("live_smoke_cleanup_failed")
    return {
        "task_active_token_count": active_task_tokens,
        "synthetic_upstream_row_count": 0,
        "upstream_row_count": len(final_items),
        "upstream_state_restored": True,
    }


def _matrix(  # noqa: PLR0915
    mode: str,
    image_digest: str,
    repetitions: int,
) -> dict[str, object]:
    if mode not in {"one-key", "two-key"}:
        raise hc.CutoverError("mode_invalid")
    if _IMAGE_RE.fullmatch(image_digest) is None:
        raise hc.CutoverError("image_digest_invalid")
    run_id = uuid4().hex
    task_label_prefix = f"live-smoke-run:{run_id}:"
    log_since = datetime.now(UTC).isoformat()
    settings = hc.Settings.production()
    running_ref = _run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", "nvidia-build-lb-app-1"]
    ).strip()
    if not running_ref.endswith(f"@{image_digest}"):
        raise hc.CutoverError("deployed_image_digest_mismatch")
    items = _upstreams(settings)
    if len(items) != 2:
        raise hc.CutoverError("exactly_two_upstream_rows_required")
    initial_enabled = _upstream_enabled_projection(items)
    all_key_ids = [str(item["id"]) for item in items]
    key_ids = [
        str(item["id"])
        for item in items
        if item.get("enabled") is True and item.get("routing_state") == "eligible"
    ]
    expected_keys = 1 if mode == "one-key" else 2
    if len(key_ids) != expected_keys:
        raise hc.CutoverError("eligible_key_count_invalid")
    issued: list[IssuedToken] = []
    receipt: dict[str, object] | None = None
    synthetic_id: str | None = None
    cleanup_receipt: dict[str, object] | None = None
    try:
        smoke = _issue(
            settings,
            f"{task_label_prefix}primary",
            ["models:read", "chat:write"],
        )
        issued.append(smoke)
        per_key = _per_key_calls(settings, smoke.bearer, key_ids)
        scope = _scope_and_revoke(settings, issued, task_label_prefix)
        core: list[dict[str, bool]] = []
        for _ in range(repetitions):
            _models(settings, smoke.bearer)
            _chat(settings, smoke.bearer, stream=False)
            _chat(settings, smoke.bearer, stream=True)
            core.append({"models": True, "nonstream": True, "stream": True})
        round_robin = (
            _round_robin_sequence(settings, smoke.bearer, key_ids) if mode == "two-key" else None
        )
        controlled = (
            _controlled_failure(settings, smoke.bearer, key_ids) if mode == "two-key" else None
        )
        cooldown = (
            _controlled_cooldown(settings, smoke.bearer, key_ids) if mode == "two-key" else None
        )
        if controlled is not None:
            controlled_id = controlled.get("synthetic_key_id")
            if not isinstance(controlled_id, str):
                raise hc.CutoverError("controlled_failure_receipt_invalid")
            synthetic_id = controlled_id
        restart = _restart_persistence(
            settings,
            running_ref,
            all_key_ids,
            key_ids,
            smoke.bearer,
            log_since,
        )
        scan = _secret_safe_runtime_scan(log_since)
        receipt = {
            "schema_version": 1,
            "status": "PASS",
            "mode": mode,
            "image_digest": image_digest,
            "key_ids": key_ids,
            "per_key_success": per_key,
            "round_robin": round_robin,
            "round_robin_deltas": (
                round_robin.get("counter_deltas") if round_robin is not None else None
            ),
            "controlled_failure": controlled,
            "controlled_cooldown": cooldown,
            "scope_and_revoke": scope,
            "core_repetitions": core,
            "restart_persistence": restart,
            "secret_scan": scan,
        }
    finally:
        cleanup_receipt = _cleanup_matrix(
            settings,
            issued,
            key_ids,
            initial_enabled,
            synthetic_id,
            task_label_prefix,
        )
    if receipt is None or cleanup_receipt is None:
        raise hc.CutoverError("live_smoke_receipt_missing")
    receipt["cleanup"] = cleanup_receipt
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("one-key", "two-key"))
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--repetitions", type=int, default=3, choices=range(1, 11))
    return parser


def main() -> int:
    """Run the matrix and emit one secret-free JSON receipt."""
    arguments = _parser().parse_args()
    if os.geteuid() != 0:
        print(json.dumps({"status": "FAIL", "error": "root_required"}))
        return 1

    def interrupted(_signum: int, _frame: object) -> None:
        raise hc.CutoverError("live_smoke_interrupted")

    prior_handlers = {
        signum: signal.signal(signum, interrupted)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    }
    try:
        receipt = _matrix(arguments.mode, arguments.image_digest, arguments.repetitions)
    except hc.CutoverError as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, separators=(",", ":")))
        return 1
    except Exception:
        print(json.dumps({"status": "FAIL", "error": "unexpected_runtime_error"}))
        return 1
    finally:
        for signum, handler in prior_handlers.items():
            _ = signal.signal(signum, handler)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
