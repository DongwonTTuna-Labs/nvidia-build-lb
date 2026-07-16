"""Production browser capture and independent-review binding tests."""

import hashlib
import json
from pathlib import Path

import pytest

from .browser_evidence import CaptureIndex, CaptureRecord
from .browser_prod_verify import (
    _fresh_cleanup_binding,  # pyright: ignore[reportPrivateUsage]
    _immutable_review_request,  # pyright: ignore[reportPrivateUsage]
    _review_is_valid,  # pyright: ignore[reportPrivateUsage]
    _ReviewRequest,  # pyright: ignore[reportPrivateUsage]
    _RunArtifactBinding,  # pyright: ignore[reportPrivateUsage]
    _RunArtifactHashes,  # pyright: ignore[reportPrivateUsage]
    _verified_capture_index,  # pyright: ignore[reportPrivateUsage]
    _VisualReview,  # pyright: ignore[reportPrivateUsage]
)


def _capture_index(run: Path, *, content: bytes = b"png bytes") -> CaptureIndex:
    captures = run / "captures"
    captures.mkdir(parents=True)
    artifact = (captures / "admin-default.png").absolute()
    _ = artifact.write_bytes(content)
    receipt = CaptureIndex(
        captures=(
            CaptureRecord(
                name="admin-default",
                route="http://127.0.0.1:2456/admin",
                state="default",
                viewport="1280x900",
                reduced_motion=False,
                native_zoom=False,
                path=str(artifact),
                sha256=hashlib.sha256(content).hexdigest(),
                byte_count=len(content),
                source_newest_mtime_ns=1,
                capture_mtime_ns=2,
                source_paths=("src/nvidia_build_lb/web/templates/admin.html",),
                pixel_width=1280,
                pixel_height=900,
                landmarks=("right-edge:1280", "bottom-edge:900"),
                content_row_coverage=1,
            ),
        )
    )
    _ = (run / "capture-index.json").write_text(
        receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def _write_capture_index(run: Path, receipt: CaptureIndex) -> None:
    _ = (run / "capture-index.json").write_text(
        receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def test_verifier_rehashes_capture_bytes_on_every_invocation(tmp_path: Path) -> None:
    _ = _capture_index(tmp_path)
    observed, index_digest = _verified_capture_index(tmp_path)
    index_bytes = (tmp_path / "capture-index.json").read_bytes()
    assert observed.captures[0].name == "admin-default"
    assert index_digest == hashlib.sha256(index_bytes).hexdigest()

    _ = (tmp_path / "captures/admin-default.png").write_bytes(b"png byteS")

    with pytest.raises(AssertionError, match="capture bytes changed"):
        _ = _verified_capture_index(tmp_path)


def test_verifier_rejects_missing_capture(tmp_path: Path) -> None:
    _ = _capture_index(tmp_path)
    (tmp_path / "captures/admin-default.png").unlink()

    with pytest.raises(AssertionError, match="entry set changed"):
        _ = _verified_capture_index(tmp_path)


def test_verifier_rejects_unindexed_capture_directory_entries(tmp_path: Path) -> None:
    _ = _capture_index(tmp_path)
    _ = (tmp_path / "captures/unreviewed.png").write_bytes(b"unreviewed")

    with pytest.raises(AssertionError, match="entry set changed"):
        _ = _verified_capture_index(tmp_path)


def test_verifier_rejects_record_path_outside_run(tmp_path: Path) -> None:
    receipt = _capture_index(tmp_path)
    outside = (tmp_path.parent / "outside.png").absolute()
    changed = receipt.model_copy(
        update={
            "captures": (receipt.captures[0].model_copy(update={"path": str(outside)}),),
        }
    )
    _write_capture_index(tmp_path, changed)

    with pytest.raises(AssertionError, match="not bound to its run"):
        _ = _verified_capture_index(tmp_path)


def test_verifier_rejects_png_and_index_symlinks(tmp_path: Path) -> None:
    _ = _capture_index(tmp_path)
    artifact = tmp_path / "captures/admin-default.png"
    outside_png = tmp_path / "outside.png"
    _ = outside_png.write_bytes(artifact.read_bytes())
    artifact.unlink()
    _ = artifact.symlink_to(outside_png)

    with pytest.raises(OSError, match=r"(Too many levels|Not a directory)"):
        _ = _verified_capture_index(tmp_path)

    artifact.unlink()
    _ = artifact.write_bytes(b"png bytes")
    index = tmp_path / "capture-index.json"
    outside_index = tmp_path / "outside-index.json"
    _ = outside_index.write_bytes(index.read_bytes())
    index.unlink()
    _ = index.symlink_to(outside_index)

    with pytest.raises(OSError, match=r"(Too many levels|Not a directory)"):
        _ = _verified_capture_index(tmp_path)


def test_verifier_rejects_symlinked_capture_directory_or_run(tmp_path: Path) -> None:
    direct_run = tmp_path / "direct-run"
    _ = _capture_index(direct_run)
    real_captures = direct_run / "real-captures"
    _ = (direct_run / "captures").rename(real_captures)
    _ = (direct_run / "captures").symlink_to(real_captures, target_is_directory=True)

    with pytest.raises(OSError, match=r"(Too many levels|Not a directory)"):
        _ = _verified_capture_index(direct_run)

    real_run = tmp_path / "real-run"
    _ = _capture_index(real_run)
    linked_run = tmp_path / "linked-run"
    _ = linked_run.symlink_to(real_run, target_is_directory=True)

    with pytest.raises(OSError, match=r"(Too many levels|Not a directory)"):
        _ = _verified_capture_index(linked_run)


def test_visual_review_is_bound_to_both_capture_index_hashes(tmp_path: Path) -> None:
    binding: _RunArtifactBinding = {
        "run-a": _RunArtifactHashes(
            adversarial="a" * 64,
            candidate="b" * 64,
            capture_index="c" * 64,
            lighthouse="d" * 64,
            manual_qa="e" * 64,
            stack_cleanup="f" * 64,
        ),
        "run-b": _RunArtifactHashes(
            adversarial="1" * 64,
            candidate="2" * 64,
            capture_index="3" * 64,
            lighthouse="4" * 64,
            manual_qa="5" * 64,
            stack_cleanup="6" * 64,
        ),
    }
    review = _VisualReview(
        blocking_findings=(),
        fresh_cleanup_sha256="0" * 64,
        image_digest="sha256:" + "c" * 64,
        postgres_image_digest="sha256:" + "7" * 64,
        lane="objective-visual",
        process_baseline_sha256="f" * 64,
        review_request_sha256="8" * 64,
        run_artifact_sha256=binding,
        schema_version=2,
        source_tree_sha256="d" * 64,
        status="PASS",
    )
    path = tmp_path / "visual-review-a.json"
    _ = path.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")
    assert _review_is_valid(path, review)

    changed_binding: _RunArtifactBinding = {
        **binding,
        "run-a": binding["run-a"].model_copy(update={"manual_qa": "0" * 64}),
    }
    changed = review.model_copy(update={"run_artifact_sha256": changed_binding})
    assert not _review_is_valid(path, changed)
    changed_baseline = review.model_copy(update={"process_baseline_sha256": "e" * 64})
    assert not _review_is_valid(path, changed_baseline)
    changed_cleanup = review.model_copy(update={"fresh_cleanup_sha256": "1" * 64})
    assert not _review_is_valid(path, changed_cleanup)
    changed_request = review.model_copy(update={"review_request_sha256": "9" * 64})
    assert not _review_is_valid(path, changed_request)

    for stale_binding in (
        {"run-a": binding["run-a"]},
        {"run-a": binding["run-b"], "run-b": binding["run-a"]},
    ):
        stale = review.model_copy(update={"run_artifact_sha256": stale_binding})
        assert not _review_is_valid(path, stale)


def test_visual_review_symlink_is_never_accepted(tmp_path: Path) -> None:
    run_hashes = _RunArtifactHashes(
        adversarial="a" * 64,
        candidate="b" * 64,
        capture_index="c" * 64,
        lighthouse="d" * 64,
        manual_qa="e" * 64,
        stack_cleanup="f" * 64,
    )
    review = _VisualReview(
        blocking_findings=(),
        fresh_cleanup_sha256="0" * 64,
        image_digest="sha256:" + "c" * 64,
        postgres_image_digest="sha256:" + "7" * 64,
        lane="objective-visual",
        process_baseline_sha256="f" * 64,
        review_request_sha256="8" * 64,
        run_artifact_sha256={"run-a": run_hashes, "run-b": run_hashes},
        schema_version=2,
        source_tree_sha256="d" * 64,
        status="PASS",
    )
    outside = tmp_path / "outside-review.json"
    _ = outside.write_text(review.model_dump_json(indent=2) + "\n", encoding="utf-8")
    path = tmp_path / "visual-review-a.json"
    _ = path.symlink_to(outside)

    assert not _review_is_valid(path, review)


def test_review_request_is_created_once_and_resume_never_rewrites_it(tmp_path: Path) -> None:
    run_hashes = _RunArtifactHashes(
        adversarial="a" * 64,
        candidate="b" * 64,
        capture_index="c" * 64,
        lighthouse="d" * 64,
        manual_qa="e" * 64,
        stack_cleanup="f" * 64,
    )
    request = _ReviewRequest(
        fresh_cleanup_sha256=None,
        image_digest="sha256:" + "1" * 64,
        postgres_image_digest="sha256:" + "2" * 64,
        process_baseline_sha256="3" * 64,
        run_artifact_sha256={"run-a": run_hashes, "run-b": run_hashes},
        schema_version=2,
        source_tree_sha256="4" * 64,
        status="REVIEW_REQUIRED",
    )

    observed, initial_sha = _immutable_review_request(tmp_path, request, resume=False)
    path = tmp_path / "review-request.json"
    initial_bytes = path.read_bytes()
    resumed, resumed_sha = _immutable_review_request(tmp_path, request, resume=True)

    assert observed == request
    assert resumed == request
    assert resumed_sha == initial_sha
    assert path.read_bytes() == initial_bytes

    _ = path.write_bytes(initial_bytes + b"\n")
    _, drifted_sha = _immutable_review_request(tmp_path, request, resume=True)
    assert drifted_sha != initial_sha
    assert path.read_bytes() == initial_bytes + b"\n"


def test_fresh_cleanup_binding_requires_pass_and_exact_zero_observations(tmp_path: Path) -> None:
    receipt: dict[str, object] = {
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
    }
    path = tmp_path / "cleanup-fresh.json"
    payload = json.dumps(receipt, sort_keys=True).encode()
    _ = path.write_bytes(payload)
    assert _fresh_cleanup_binding(tmp_path) == hashlib.sha256(payload).hexdigest()

    receipt["status"] = "FAIL"
    _ = path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="PASS"):
        _ = _fresh_cleanup_binding(tmp_path)
