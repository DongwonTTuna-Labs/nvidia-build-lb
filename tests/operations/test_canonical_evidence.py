"""Closed canonical evidence aggregation across the app/PostgreSQL pair."""

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from scripts.qa.canonical_evidence import Arguments, build_receipt

_SOURCE = "a" * 64
_APP = "sha256:" + "b" * 64
_POSTGRES = "sha256:" + "c" * 64
_JSON_OBJECT = TypeAdapter(dict[str, object])
_V3_FIELDS: dict[str, object] = {
    "admin_event_count": 4,
    "admin_event_identity_sha256": "4" * 64,
    "attempt_receipt_count": 2,
    "pending_attempt_count": 1,
    "attempt_receipt_identity_sha256": "5" * 64,
    "live_pin_count": 1,
    "live_pin_identity_sha256": "6" * 64,
    "rolled_up_routed_request_count": 7,
    "admin_ledger_state_sha256": "7" * 64,
}


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _read(path: Path) -> dict[str, object]:
    return _JSON_OBJECT.validate_json(path.read_bytes())


def _pass(**extra: object) -> dict[str, object]:
    return {"status": "PASS", **extra}


def _arguments(root: Path) -> Arguments:
    arguments = Arguments()
    arguments.todo6a = root / "todo6a"
    arguments.todo6b = root / "todo6b"
    arguments.verify_local = root / "verify"
    arguments.scan_release = root / "scan"
    arguments.runtime_audit = root / "runtime-audit.json"
    arguments.output = root / "canonical.json"
    return arguments


def _runtime_hypotheses(hashes: tuple[str, str, str]) -> list[dict[str, object]]:
    return [
        {
            "id": f"hypothesis-{index}",
            "hypothesis": f"Failure hypothesis {index}",
            "observation": f"Observed evidence {index}",
            "result": "DISPROVED",
            "artifact_sha256": [artifact_hash],
        }
        for index, artifact_hash in enumerate(hashes)
    ]


def _build_fixture(root: Path) -> Arguments:
    arguments = _arguments(root)
    source: dict[str, object] = {
        "schema_version": 1,
        "algorithm": "git-files-type-canonical-mode-path-payload-sha256-v2",
        "source_tree_sha256": _SOURCE,
        "entry_count": 0,
        "entries": [],
    }
    for directory in (
        arguments.todo6a,
        arguments.todo6b,
        arguments.verify_local,
        arguments.scan_release,
    ):
        _write(directory / "source-manifest.json", source)
    pair = {
        "source_tree_sha256": _SOURCE,
        "image_digest": _APP,
        "postgres_image_digest": _POSTGRES,
    }
    for name in ("candidate.json", "manual-qa.json", "adversarial.json", "cleanup.json"):
        _write(arguments.todo6a / name, _pass(**pair))
    _write(arguments.todo6a / "source-snapshot.json", {"status": "PASS"})

    run_bindings: dict[str, dict[str, str]] = {}
    run_files = {
        "adversarial": "adversarial.json",
        "candidate": "candidate.json",
        "capture_index": "capture-index.json",
        "lighthouse": "lighthouse.json",
        "manual_qa": "manual-qa.json",
        "stack_cleanup": "stack-cleanup.json",
    }
    for run_name in ("run-a", "run-b"):
        run = arguments.todo6b / "runs" / run_name
        for filename in run_files.values():
            _write(run / filename, {"run": run_name, "file": filename})
        run_bindings[run_name] = {
            key: hashlib.sha256((run / filename).read_bytes()).hexdigest()
            for key, filename in run_files.items()
        }
    _write(arguments.todo6b / "process-baseline.json", {"schema_version": 1})
    _write(arguments.todo6b / "cleanup-fresh.json", _pass(**pair))
    process_sha = hashlib.sha256(
        (arguments.todo6b / "process-baseline.json").read_bytes()
    ).hexdigest()
    cleanup_sha = hashlib.sha256((arguments.todo6b / "cleanup-fresh.json").read_bytes()).hexdigest()
    request = {
        "status": "REVIEW_REQUIRED",
        "schema_version": 2,
        **pair,
        "process_baseline_sha256": process_sha,
        "fresh_cleanup_sha256": None,
        "run_artifact_sha256": run_bindings,
    }
    _write(arguments.todo6b / "review-request.json", request)
    request_sha = hashlib.sha256(
        (arguments.todo6b / "review-request.json").read_bytes()
    ).hexdigest()
    review_common: dict[str, object] = {
        "status": "PASS",
        "schema_version": 2,
        **pair,
        "process_baseline_sha256": process_sha,
        "fresh_cleanup_sha256": cleanup_sha,
        "review_request_sha256": request_sha,
        "run_artifact_sha256": run_bindings,
        "blocking_findings": [],
    }
    _write(
        arguments.todo6b / "visual-review-a.json",
        review_common | {"lane": "objective-visual"},
    )
    _write(
        arguments.todo6b / "visual-review-b.json",
        review_common | {"lane": "design-accessibility-persona"},
    )
    for name in (
        "candidate.json",
        "determinism.json",
        "cleanup-resume.json",
        "cleanup.json",
    ):
        _write(arguments.todo6b / name, _pass(**pair))
    _write(arguments.todo6b / "source-snapshot.json", {"status": "PASS"})

    backup_state = {
        "schema_version": 2,
        "pair_id": "f" * 64,
        "backup_id": "backup-v2",
        "alembic_revision": "0004_vault_key_verifier",
        "upstream_count": 1,
        "upstream_identity_sha256": "1" * 64,
        "downstream_count": 1,
        "downstream_digest_sha256": "2" * 64,
        "vault_key_sha256": "3" * 64,
        "vault_key_matches_database": True,
    }
    _write(arguments.verify_local / "manual-qa.json", _pass(**pair))
    _write(arguments.verify_local / "adversarial.json", _pass())
    _write(arguments.verify_local / "cleanup.json", _pass())
    _write(
        arguments.verify_local / "backup.json", _pass(**backup_state, restored_state_matches=False)
    )
    _write(
        arguments.verify_local / "restore.json", _pass(**backup_state, restored_state_matches=True)
    )
    _write(arguments.scan_release / "manual-qa.json", _pass(**pair))
    _write(arguments.scan_release / "adversarial.json", _pass())
    _write(arguments.scan_release / "cleanup.json", _pass())
    bound_hash = hashlib.sha256((arguments.todo6a / "candidate.json").read_bytes()).hexdigest()
    _write(
        arguments.runtime_audit,
        _pass(
            schema_version=2,
            **pair,
            hypotheses=_runtime_hypotheses((bound_hash, bound_hash, bound_hash)),
        ),
    )
    return arguments


def _promote_backup_pair_to_v3(arguments: Arguments) -> None:
    for name, restored in (("backup.json", False), ("restore.json", True)):
        path = arguments.verify_local / name
        receipt = _read(path)
        receipt.update(
            {
                "schema_version": 3,
                "alembic_revision": "0005_admin_dashboard_ledger",
                "restored_state_matches": restored,
                **_V3_FIELDS,
            }
        )
        _write(path, receipt)


def test_canonical_receipt_binds_all_source_and_image_pair_evidence(tmp_path: Path) -> None:
    arguments = _build_fixture(tmp_path)

    receipt = build_receipt(arguments)

    assert receipt.source_tree_sha256 == _SOURCE
    assert receipt.image_digest == _APP
    assert receipt.postgres_image_digest == _POSTGRES
    todo6b = receipt.evidence["todo6b"]
    assert isinstance(todo6b, dict)
    runs = todo6b["runs"]
    assert isinstance(runs, dict)
    run_a = runs["run-a"]
    assert isinstance(run_a, dict)
    assert run_a
    verify_local = receipt.evidence["verify_local"]
    assert isinstance(verify_local, dict)
    assert verify_local["backup.json"]
    assert receipt.evidence["runtime_audit_sha256"]


def test_canonical_receipt_rejects_one_source_manifest_byte_change(tmp_path: Path) -> None:
    arguments = _build_fixture(tmp_path)
    _write(arguments.scan_release / "source-manifest.json", {"source_tree_sha256": "0" * 64})

    with pytest.raises(ValueError, match="source_manifest_bytes_mismatch"):
        _ = build_receipt(arguments)


def test_canonical_receipt_rejects_runtime_audit_with_unbound_artifact_hash(
    tmp_path: Path,
) -> None:
    arguments = _build_fixture(tmp_path)
    bound_hash = hashlib.sha256((arguments.todo6a / "candidate.json").read_bytes()).hexdigest()
    _write(
        arguments.runtime_audit,
        _pass(
            schema_version=2,
            source_tree_sha256=_SOURCE,
            image_digest=_APP,
            postgres_image_digest=_POSTGRES,
            hypotheses=_runtime_hypotheses(("0" * 64, bound_hash, bound_hash)),
        ),
    )

    with pytest.raises(ValueError, match="runtime_audit_artifact_unbound"):
        _ = build_receipt(arguments)


def test_canonical_receipt_accepts_exact_v3_backup_restore_pair(tmp_path: Path) -> None:
    arguments = _build_fixture(tmp_path)
    _promote_backup_pair_to_v3(arguments)

    receipt = build_receipt(arguments)

    assert receipt.status == "PASS"


@pytest.mark.parametrize("field", tuple(_V3_FIELDS))
def test_canonical_receipt_rejects_each_missing_v3_field(
    tmp_path: Path,
    field: str,
) -> None:
    arguments = _build_fixture(tmp_path)
    _promote_backup_pair_to_v3(arguments)
    path = arguments.verify_local / "restore.json"
    receipt = _read(path)
    _ = receipt.pop(field)
    _write(path, receipt)

    with pytest.raises(ValueError, match=field):
        _ = build_receipt(arguments)


@pytest.mark.parametrize("field", tuple(_V3_FIELDS))
def test_canonical_receipt_rejects_each_v3_field_drift(
    tmp_path: Path,
    field: str,
) -> None:
    arguments = _build_fixture(tmp_path)
    _promote_backup_pair_to_v3(arguments)
    path = arguments.verify_local / "restore.json"
    receipt = _read(path)
    value = receipt[field]
    receipt[field] = value + 1 if isinstance(value, int) else "8" * 64
    _write(path, receipt)

    with pytest.raises(ValueError, match="backup_restore_state_mismatch"):
        _ = build_receipt(arguments)


def test_canonical_receipt_rejects_backup_restore_schema_version_mismatch(
    tmp_path: Path,
) -> None:
    arguments = _build_fixture(tmp_path)
    restore = arguments.verify_local / "restore.json"
    receipt = _read(restore)
    receipt.update(
        {
            "schema_version": 3,
            "alembic_revision": "0005_admin_dashboard_ledger",
            **_V3_FIELDS,
        }
    )
    _write(restore, receipt)

    with pytest.raises(ValueError, match="backup_restore_state_mismatch"):
        _ = build_receipt(arguments)


def test_canonical_receipt_rejects_review_request_byte_drift(tmp_path: Path) -> None:
    arguments = _build_fixture(tmp_path)
    request = arguments.todo6b / "review-request.json"
    _ = request.write_bytes(request.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="visual_review_mismatch"):
        _ = build_receipt(arguments)
