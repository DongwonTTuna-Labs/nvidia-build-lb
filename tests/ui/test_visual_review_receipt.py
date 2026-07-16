"""Artifact-bound visual-review receipt command contracts."""

import hashlib
import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts/qa/record_visual_review.py"
_SOURCE_SHA = "1" * 64
_IMAGE = f"sha256:{'2' * 64}"
_POSTGRES_IMAGE = f"sha256:{'3' * 64}"


class _Receipt(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    blocking_findings: tuple[str, ...]
    fresh_cleanup_sha256: str
    image_digest: str
    postgres_image_digest: str
    lane: Literal["objective-visual", "design-accessibility-persona"]
    process_baseline_sha256: str
    review_request_sha256: str
    run_artifact_sha256: dict[str, dict[str, str]]
    schema_version: Literal[2]
    source_tree_sha256: str
    status: Literal["PASS"]


def _write(path: Path, payload: object) -> bytes:
    content = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    _ = path.write_bytes(content)
    return content


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_artifacts(evidence: Path, run_name: str) -> dict[str, str]:
    run = evidence / "runs" / run_name
    captures = run / "captures"
    captures.mkdir(parents=True)
    image = b"secret-free-raster"
    image_path = (captures / "overview.png").absolute()
    _ = image_path.write_bytes(image)
    capture_index = {
        "captures": [
            {
                "name": "overview",
                "route": "http://127.0.0.1:2456/admin",
                "state": "ready",
                "viewport": "1280x900",
                "reduced_motion": False,
                "native_zoom": False,
                "path": str(image_path),
                "sha256": hashlib.sha256(image).hexdigest(),
                "byte_count": len(image),
                "source_newest_mtime_ns": 1,
                "capture_mtime_ns": 2,
                "source_paths": ["src/nvidia_build_lb/web/static/admin.css"],
                "pixel_width": 1,
                "pixel_height": 1,
                "landmarks": ["right-edge:1", "bottom-edge:1"],
                "content_row_coverage": 1.0,
            }
        ]
    }
    files = {
        "adversarial": "adversarial.json",
        "candidate": "candidate.json",
        "capture_index": "capture-index.json",
        "lighthouse": "lighthouse.json",
        "manual_qa": "manual-qa.json",
        "stack_cleanup": "stack-cleanup.json",
    }
    for field, filename in files.items():
        payload = capture_index if field == "capture_index" else {"field": field}
        _ = _write(run / filename, payload)
    return {field: _sha(run / filename) for field, filename in files.items()}


def _evidence(tmp_path: Path) -> tuple[Path, str, dict[str, dict[str, str]]]:
    evidence = tmp_path / "browser"
    evidence.mkdir()
    process = _write(evidence / "process-baseline.json", {"schema_version": 1})
    cleanup = _write(
        evidence / "cleanup-fresh.json",
        {
            "schema_version": 1,
            "status": "PASS",
            "trigger_exit_status": 75,
            "final_exit_status": 75,
            "remaining": {
                "containers": 0,
                "networks": 0,
                "volumes": 0,
                "port_listeners": 0,
                "temp_secret_directories": 0,
                "temp_client_directories": 0,
                "browser_temporary_directories": 0,
                "temporary_postgres_images": 0,
                "browser_processes": 0,
                "playwright_drivers": 0,
                "lighthouse_processes": 0,
            },
        },
    )
    _ = _write(
        evidence / "source-manifest.json",
        {"schema_version": 1, "source_tree_sha256": _SOURCE_SHA},
    )
    bindings = {
        "run-a": _run_artifacts(evidence, "run-a"),
        "run-b": _run_artifacts(evidence, "run-b"),
    }
    request = _write(
        evidence / "review-request.json",
        {
            "schema_version": 2,
            "status": "REVIEW_REQUIRED",
            "fresh_cleanup_sha256": None,
            "image_digest": _IMAGE,
            "postgres_image_digest": _POSTGRES_IMAGE,
            "process_baseline_sha256": hashlib.sha256(process).hexdigest(),
            "run_artifact_sha256": bindings,
            "source_tree_sha256": _SOURCE_SHA,
        },
    )
    return (
        evidence,
        hashlib.sha256(request).hexdigest(),
        bindings | {"cleanup": {"sha256": hashlib.sha256(cleanup).hexdigest()}},
    )


def _invoke(evidence: Path, request_sha: str, lane: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed repository QA helper.
        [
            sys.executable,
            _SCRIPT,
            "--evidence-dir",
            evidence,
            "--lane",
            lane,
            "--review-request-sha256",
            request_sha,
            "--verdict",
            "lgtm",
        ],
        cwd=_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_record_visual_review_binds_exact_request_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    evidence, request_sha, bindings = _evidence(tmp_path)
    request_bytes = (evidence / "review-request.json").read_bytes()

    created = _invoke(evidence, request_sha, "objective-visual")
    receipt_path = evidence / "visual-review-a.json"
    receipt_bytes = receipt_path.read_bytes()
    receipt = _Receipt.model_validate_json(receipt_bytes)

    assert created.returncode == 0
    assert created.stdout == ""
    assert created.stderr == ""
    assert receipt.lane == "objective-visual"
    assert receipt.blocking_findings == ()
    assert receipt.source_tree_sha256 == _SOURCE_SHA
    assert receipt.image_digest == _IMAGE
    assert receipt.postgres_image_digest == _POSTGRES_IMAGE
    assert receipt.review_request_sha256 == request_sha
    assert receipt.run_artifact_sha256 == {
        "run-a": bindings["run-a"],
        "run-b": bindings["run-b"],
    }
    assert receipt.fresh_cleanup_sha256 == bindings["cleanup"]["sha256"]
    assert (evidence / "review-request.json").read_bytes() == request_bytes
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600

    overwritten = _invoke(evidence, request_sha, "objective-visual")
    wrong_request = _invoke(evidence, "f" * 64, "design-accessibility-persona")

    assert overwritten.returncode == 1
    assert overwritten.stderr == "visual_review_receipt_failed\n"
    assert receipt_path.read_bytes() == receipt_bytes
    assert wrong_request.returncode == 1
    assert wrong_request.stderr == "visual_review_receipt_failed\n"
    assert not (evidence / "visual-review-b.json").exists()
