#!/usr/bin/env python3
"""Run the Rust gateway live matrix without touching provider secrets.

The operator supplies an already deployed, immutable image.  This script only
uses the current admin API contract; it never edits PostgreSQL tables and never
prints a credential or provider response body.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from scripts.ops import hermes_cutover as hc

_IMAGE_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DOWNSTREAM_RE: Final[re.Pattern[str]] = re.compile(r"nblb_ds_[0-9a-f]{64}\Z")
_CREDENTIAL_RE: Final[re.Pattern[bytes]] = re.compile(rb"nvapi-[A-Za-z0-9_-]{20,}")
_HOST: Final[str] = "127.0.0.1:2456"
_PROFILES: Final[tuple[str, ...]] = (
    "z-ai/glm-5.2",
    "microsoft/phi-4-multimodal-instruct",
    "nvidia/vila",
    "nvidia/nvclip",
    "black-forest-labs/flux.1-kontext-dev",
    "stabilityai/stable-video-diffusion",
    "nvidia/magpie-tts-multilingual",
    "nvidia/parakeet-ctc-1.1b",
)
_SECRET_FILES: Final[tuple[Path, ...]] = (
    Path("/opt/nvidia-build-lb/secrets/admin_token"),
    Path("/opt/nvidia-build-lb/secrets/vault_master_key"),
    Path("/opt/nvidia-build-lb/secrets/db_password"),
)


class LiveSmokeError(RuntimeError):
    """Secret-free, operator-facing failure."""


@dataclass(frozen=True)
class IssuedToken:
    token_id: str
    bearer: str


def _run(command: list[str], timeout: int = 60) -> str:
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise LiveSmokeError(f"command_failed:{Path(command[0]).name}")
    return completed.stdout


def _settings() -> hc.Settings:
    return hc.Settings.production()


def _admin(settings: hc.Settings, method: str, path: str, *, payload: dict[str, object] | None = None,
           expected: tuple[int, ...] = (200,)) -> dict[str, object]:
    return hc._admin(settings, method, path, payload=payload, expected=expected)


def _keys(settings: hc.Settings) -> list[dict[str, object]]:
    items = _admin(settings, "GET", "/upstream-keys").get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise LiveSmokeError("upstream_list_invalid")
    return items


def _clients(settings: hc.Settings) -> list[dict[str, object]]:
    items = _admin(settings, "GET", "/downstream-credentials").get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise LiveSmokeError("downstream_list_invalid")
    return items


def _issue(settings: hc.Settings, label: str) -> IssuedToken:
    response = _admin(
        settings,
        "POST",
        "/downstream-credentials",
        payload={"label": label, "scopes": ["models:read", "chat:write", "embeddings:write", "images:write", "audio:write", "media:write"]},
        expected=(201,),
    )
    token_id = response.get("id")
    bearer = response.get("token")
    if not isinstance(token_id, str) or not isinstance(bearer, str) or _DOWNSTREAM_RE.fullmatch(bearer) is None:
        raise LiveSmokeError("downstream_issue_response_invalid")
    UUID(token_id)
    return IssuedToken(token_id, bearer)


def _revoke(settings: hc.Settings, token: IssuedToken) -> None:
    _admin(settings, "POST", f"/downstream-credentials/{token.token_id}/revoke", expected=(200,))
    if any(item.get("id") == token.token_id and item.get("active") for item in _clients(settings)):
        raise LiveSmokeError("downstream_revoke_not_persisted")


def _request(settings: hc.Settings, token: str, path: str, payload: dict[str, object], expected: tuple[int, ...] = (200,)) -> bytes:
    _, body = hc._request("POST", f"{settings.lb_base_url}{path}", token, payload=payload, expected=expected, timeout=360, host=_HOST)
    return body


def _chat(settings: hc.Settings, token: str, stream: bool = False, expected: tuple[int, ...] = (200,)) -> None:
    body = _request(settings, token, "/v1/chat/completions", {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": "라이브 스모크 성공"}],
        "stream": stream,
        "max_tokens": 16,
    }, expected)
    if expected != (200,):
        return
    if stream:
        if body.count(b"data: [DONE]\n\n") != 1 or not body.endswith(b"data: [DONE]\n\n"):
            raise LiveSmokeError("stream_contract_failed")
        return
    document = hc._json_body(body)
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LiveSmokeError("chat_contract_failed")


def _models(settings: hc.Settings, token: str) -> None:
    media, body = hc._request("GET", f"{settings.lb_base_url}/v1/models", token, expected=(200,), timeout=30, host=_HOST)
    document = hc._json_body(body)
    if media != "application/json" or not isinstance(document.get("data"), list) or len(document["data"]) != len(_PROFILES):
        raise LiveSmokeError("models_contract_failed")


def _multimodal(settings: hc.Settings, token: str) -> dict[str, bool]:
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    requests: tuple[tuple[str, dict[str, object]], ...] = (
        ("embeddings", {"model": "nvidia/nvclip", "input": "live smoke"}),
        ("images", {"model": "black-forest-labs/flux.1-kontext-dev", "prompt": "a green square"}),
        ("audio", {"model": "nvidia/magpie-tts-multilingual", "input": "live smoke"}),
        ("video", {"model": "stabilityai/stable-video-diffusion", "input": {"image": image}}),
    )
    result: dict[str, bool] = {}
    for name, payload in requests:
        path = {"embeddings": "/v1/embeddings", "images": "/v1/images/generations", "audio": "/v1/audio/speech", "video": "/v1/nvidia/inference"}[name]
        _request(settings, token, path, payload)
        result[name] = True
    return result


def _counts(items: list[dict[str, object]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        key_id, count = item.get("id"), item.get("request_count")
        if not isinstance(key_id, str) or not isinstance(count, int):
            raise LiveSmokeError("upstream_counter_invalid")
        result[key_id] = count
    return result


def _set_enabled(settings: hc.Settings, key_id: str, enabled: bool) -> None:
    _admin(settings, "POST", f"/upstream-keys/{key_id}/state", payload={"enabled": enabled}, expected=(200,))


def _distribution_and_failover(settings: hc.Settings, token: str, key_ids: list[str]) -> dict[str, object]:
    before = _counts(_keys(settings))
    for _ in range(6):
        _chat(settings, token)
    after = _counts(_keys(settings))
    deltas = {key_id: after[key_id] - before[key_id] for key_id in key_ids}
    if sum(deltas.values()) != 6 or any(value < 2 for value in deltas.values()):
        raise LiveSmokeError("round_robin_distribution_failed")
    disabled = key_ids[0]
    _set_enabled(settings, disabled, False)
    try:
        before_failover = _counts(_keys(settings))
        _chat(settings, token)
        after_failover = _counts(_keys(settings))
        if after_failover[disabled] != before_failover[disabled] or after_failover[key_ids[1]] <= before_failover[key_ids[1]]:
            raise LiveSmokeError("disabled_key_failover_failed")
    finally:
        _set_enabled(settings, disabled, True)
    return {"request_count": 6, "per_key_deltas": deltas, "disabled_key_failover": True}


def _secret_scan(settings: hc.Settings) -> dict[str, bool]:
    materials: list[bytes] = []
    for path in _SECRET_FILES:
        if path.exists():
            raw = path.read_bytes()
            materials.extend((raw, raw.rstrip(b"\r\n"), raw.hex().encode(), base64.b64encode(raw)))
    for container in ("nvidia-build-lb-app-1", "nvidia-build-lb-db-1"):
        logs = subprocess.run(["docker", "logs", "--since", "10m", container], capture_output=True, check=False, timeout=30)
        blob = logs.stdout + logs.stderr
        if _CREDENTIAL_RE.search(blob) or any(value and value in blob for value in materials):
            raise LiveSmokeError("secret_shape_in_runtime_logs")
    return {"logs": True, "credential_material_absent": True}


def _matrix(mode: str, image_digest: str, repetitions: int) -> dict[str, object]:
    if _IMAGE_RE.fullmatch(image_digest) is None:
        raise LiveSmokeError("image_digest_invalid")
    settings = _settings()
    running = _run(["docker", "inspect", "--format", "{{.Config.Image}}", "nvidia-build-lb-app-1"]).strip()
    if not running.endswith(f"@{image_digest}"):
        raise LiveSmokeError("deployed_image_digest_mismatch")
    keys = _keys(settings)
    if len(keys) != 2:
        raise LiveSmokeError("exactly_two_upstream_rows_required")
    key_ids = [str(item["id"]) for item in keys if item.get("enabled") is True and item.get("cooldown_until") is None]
    if len(key_ids) != (1 if mode == "one-key" else 2):
        raise LiveSmokeError("eligible_key_count_invalid")
    issued = _issue(settings, f"live-smoke-{os.getpid()}")
    try:
        _models(settings, issued.bearer)
        for _ in range(repetitions):
            _chat(settings, issued.bearer)
            _chat(settings, issued.bearer, stream=True)
        multimodal = _multimodal(settings, issued.bearer)
        routing = _distribution_and_failover(settings, issued.bearer, key_ids) if mode == "two-key" else None
        return {"schema_version": 2, "status": "PASS", "mode": mode, "image_digest": image_digest, "profiles": list(_PROFILES), "multimodal": multimodal, "routing": routing, "secret_scan": _secret_scan(settings)}
    finally:
        _revoke(settings, issued)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("one-key", "two-key"), required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--repetitions", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    try:
        receipt = _matrix(args.mode, args.image_digest, args.repetitions)
    except (LiveSmokeError, hc.CutoverError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, separators=(",", ":")))
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
