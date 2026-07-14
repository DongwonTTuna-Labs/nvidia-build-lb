"""Create or verify one closed canonical receipt for the full local QA pair."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

_RUN_FILES = {
    "adversarial": "adversarial.json",
    "candidate": "candidate.json",
    "capture_index": "capture-index.json",
    "lighthouse": "lighthouse.json",
    "manual_qa": "manual-qa.json",
    "stack_cleanup": "stack-cleanup.json",
}
_SCHEMA_VERSION = 2
_MINIMUM_RUNTIME_HYPOTHESES = 3
type JsonObject = dict[str, JsonValue]
_JSON_OBJECT: Final[TypeAdapter[JsonObject]] = TypeAdapter(JsonObject)
_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class Arguments(argparse.Namespace):
    """Typed command-line inputs used by the canonical evidence validator."""

    command: Literal["create", "verify"] = "create"
    output: Path = Path()
    runtime_audit: Path = Path()
    scan_release: Path = Path()
    todo6a: Path = Path()
    todo6b: Path = Path()
    verify_local: Path = Path()


class _CanonicalReceipt(_StrictModel):
    schema_version: Literal[2] = 2
    status: Literal["PASS"] = "PASS"
    source_tree_sha256: str
    source_manifest_sha256: str
    image_digest: str
    postgres_image_digest: str
    evidence: JsonObject


class _RuntimeHypothesis(_StrictModel):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
    hypothesis: Annotated[str, StringConstraints(min_length=1)]
    observation: Annotated[str, StringConstraints(min_length=1)]
    result: Literal["DISPROVED"]
    artifact_sha256: Annotated[tuple[_SHA256, ...], Field(min_length=1)]


class _RuntimeAudit(_StrictModel):
    schema_version: Literal[2]
    status: Literal["PASS"]
    source_tree_sha256: str
    image_digest: str
    postgres_image_digest: str
    hypotheses: Annotated[tuple[_RuntimeHypothesis, ...], Field(min_length=3)]

    @model_validator(mode="after")
    def validate_unique_hypotheses(self) -> Self:
        """Reject repeated hypotheses that only inflate audit cardinality."""
        if len({hypothesis.id for hypothesis in self.hypotheses}) != len(self.hypotheses):
            reason = "runtime hypothesis IDs must be unique"
            raise ValueError(reason)
        return self


def _regular_bytes(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        reason = f"non_regular_evidence:{path.name}"
        raise ValueError(reason)
    return path.read_bytes()


def _sha256(path: Path) -> str:
    return hashlib.sha256(_regular_bytes(path)).hexdigest()


def _json(path: Path) -> JsonObject:
    return _JSON_OBJECT.validate_json(_regular_bytes(path))


def _pass(path: Path) -> JsonObject:
    value = _json(path)
    if value.get("status") != "PASS":
        reason = f"non_pass_evidence:{path.name}"
        raise ValueError(reason)
    return value


def _bindings(root: Path, files: tuple[str, ...]) -> dict[str, str]:
    return {name: _sha256(root / name) for name in files}


def _run_bindings(root: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for run_name in ("run-a", "run-b"):
        run = root / "runs" / run_name
        result[run_name] = {key: _sha256(run / filename) for key, filename in _RUN_FILES.items()}
    return result


def _same_field(values: tuple[JsonObject, ...], field: str) -> str:
    observed = tuple(value.get(field) for value in values)
    if not observed or not isinstance(observed[0], str) or len(set(observed)) != 1:
        reason = f"canonical_field_mismatch:{field}"
        raise ValueError(reason)
    return observed[0]


def _validate_visual_reviews(
    todo6b: Path,
    image_digest: str,
    postgres_image_digest: str,
    source_tree_sha256: str,
    run_artifacts: dict[str, dict[str, str]],
) -> None:
    request = _pass(todo6b / "review-request.json")
    if (
        request.get("schema_version") != _SCHEMA_VERSION
        or request.get("image_digest") != image_digest
        or request.get("postgres_image_digest") != postgres_image_digest
        or request.get("source_tree_sha256") != source_tree_sha256
        or request.get("run_artifact_sha256") != run_artifacts
    ):
        reason = "visual_review_request_mismatch"
        raise ValueError(reason)
    common = {
        "schema_version": _SCHEMA_VERSION,
        "status": "PASS",
        "image_digest": image_digest,
        "postgres_image_digest": postgres_image_digest,
        "source_tree_sha256": source_tree_sha256,
        "process_baseline_sha256": request.get("process_baseline_sha256"),
        "fresh_cleanup_sha256": request.get("fresh_cleanup_sha256"),
        "run_artifact_sha256": run_artifacts,
        "blocking_findings": [],
    }
    for filename, lane in (
        ("visual-review-a.json", "objective-visual"),
        ("visual-review-b.json", "design-accessibility-persona"),
    ):
        review = _pass(todo6b / filename)
        if review != common | {"lane": lane}:
            reason = "visual_review_mismatch"
            raise ValueError(reason)


def _source_identity(arguments: Arguments) -> tuple[str, str]:
    source_paths = (
        arguments.todo6a / "source-manifest.json",
        arguments.todo6b / "source-manifest.json",
        arguments.verify_local / "source-manifest.json",
        arguments.scan_release / "source-manifest.json",
    )
    source_bytes = tuple(_regular_bytes(path) for path in source_paths)
    if len(set(source_bytes)) != 1:
        reason = "source_manifest_bytes_mismatch"
        raise ValueError(reason)
    source_manifest = _JSON_OBJECT.validate_json(source_bytes[0])
    source_tree_sha256 = source_manifest.get("source_tree_sha256")
    if not isinstance(source_tree_sha256, str):
        reason = "source_tree_sha256_invalid"
        raise TypeError(reason)
    return source_tree_sha256, hashlib.sha256(source_bytes[0]).hexdigest()


def _validate_backup_restore(verify_local: Path) -> None:
    backup = _pass(verify_local / "backup.json")
    restore = _pass(verify_local / "restore.json")
    state_fields = (
        "pair_id",
        "alembic_revision",
        "upstream_count",
        "upstream_identity_sha256",
        "downstream_count",
        "downstream_digest_sha256",
        "vault_key_sha256",
    )
    if any(backup.get(field) != restore.get(field) for field in state_fields):
        reason = "backup_restore_state_mismatch"
        raise ValueError(reason)
    if backup.get("restored_state_matches") is not False:
        reason = "backup_receipt_invalid"
        raise ValueError(reason)
    if restore.get("restored_state_matches") is not True:
        reason = "restore_receipt_invalid"
        raise ValueError(reason)


def _validate_runtime_audit(runtime_audit: JsonObject, artifact_hashes: set[str]) -> None:
    audit = _RuntimeAudit.model_validate(runtime_audit)
    if len(audit.hypotheses) < _MINIMUM_RUNTIME_HYPOTHESES:
        reason = "runtime_audit_hypotheses_missing"
        raise ValueError(reason)
    if any(
        not set(hypothesis.artifact_sha256).issubset(artifact_hashes)
        for hypothesis in audit.hypotheses
    ):
        reason = "runtime_audit_artifact_unbound"
        raise ValueError(reason)


def _artifact_hashes(
    stage_bindings: tuple[dict[str, str], ...],
    run_artifacts: dict[str, dict[str, str]],
) -> set[str]:
    hashes = {value for bindings in stage_bindings for value in bindings.values()}
    hashes.update(value for run in run_artifacts.values() for value in run.values())
    return hashes


def _validate_stage_passes(arguments: Arguments) -> None:
    stages = (
        (
            arguments.todo6a,
            ("candidate.json", "manual-qa.json", "adversarial.json", "cleanup.json"),
        ),
        (
            arguments.todo6b,
            (
                "candidate.json",
                "determinism.json",
                "review-request.json",
                "visual-review-a.json",
                "visual-review-b.json",
                "cleanup-fresh.json",
                "cleanup-resume.json",
                "cleanup.json",
            ),
        ),
        (
            arguments.verify_local,
            (
                "manual-qa.json",
                "adversarial.json",
                "cleanup.json",
                "backup.json",
                "restore.json",
            ),
        ),
        (
            arguments.scan_release,
            ("manual-qa.json", "adversarial.json", "cleanup.json"),
        ),
    )
    for root, files in stages:
        for filename in files:
            _ = _pass(root / filename)


def build_receipt(arguments: Arguments) -> _CanonicalReceipt:
    """Validate every upstream receipt and return their exact canonical binding."""
    source_tree_sha256, source_manifest_sha256 = _source_identity(arguments)

    todo6a_candidate = _pass(arguments.todo6a / "candidate.json")
    todo6b_candidate = _pass(arguments.todo6b / "candidate.json")
    verify_manual = _pass(arguments.verify_local / "manual-qa.json")
    scan_manual = _pass(arguments.scan_release / "manual-qa.json")
    runtime_audit = _pass(arguments.runtime_audit)
    pair_receipts = (
        todo6a_candidate,
        todo6b_candidate,
        verify_manual,
        scan_manual,
        runtime_audit,
    )
    image_digest = _same_field(pair_receipts, "image_digest")
    postgres_image_digest = _same_field(pair_receipts, "postgres_image_digest")
    if _same_field(pair_receipts, "source_tree_sha256") != source_tree_sha256:
        reason = "source_tree_receipt_mismatch"
        raise ValueError(reason)

    run_artifacts = _run_bindings(arguments.todo6b)
    todo6a_bindings = _bindings(
        arguments.todo6a,
        (
            "source-manifest.json",
            "source-snapshot.json",
            "candidate.json",
            "manual-qa.json",
            "adversarial.json",
            "cleanup.json",
        ),
    )
    todo6b_bindings = _bindings(
        arguments.todo6b,
        (
            "source-manifest.json",
            "source-snapshot.json",
            "process-baseline.json",
            "determinism.json",
            "candidate.json",
            "review-request.json",
            "visual-review-a.json",
            "visual-review-b.json",
            "cleanup-fresh.json",
            "cleanup-resume.json",
            "cleanup.json",
        ),
    )
    verify_bindings = _bindings(
        arguments.verify_local,
        (
            "source-manifest.json",
            "manual-qa.json",
            "adversarial.json",
            "cleanup.json",
            "backup.json",
            "restore.json",
        ),
    )
    scan_bindings = _bindings(
        arguments.scan_release,
        (
            "source-manifest.json",
            "manual-qa.json",
            "adversarial.json",
            "cleanup.json",
        ),
    )
    _validate_stage_passes(arguments)
    _validate_backup_restore(arguments.verify_local)
    _validate_runtime_audit(
        runtime_audit,
        _artifact_hashes(
            (todo6a_bindings, todo6b_bindings, verify_bindings, scan_bindings),
            run_artifacts,
        ),
    )
    _validate_visual_reviews(
        arguments.todo6b,
        image_digest,
        postgres_image_digest,
        source_tree_sha256,
        run_artifacts,
    )

    evidence = _JSON_OBJECT.validate_python(
        {
            "todo6a": todo6a_bindings,
            "todo6b": todo6b_bindings | {"runs": run_artifacts},
            "verify_local": verify_bindings,
            "scan_release": scan_bindings,
            "runtime_audit_sha256": _sha256(arguments.runtime_audit),
        }
    )
    return _CanonicalReceipt(
        source_tree_sha256=source_tree_sha256,
        source_manifest_sha256=source_manifest_sha256,
        image_digest=image_digest,
        postgres_image_digest=postgres_image_digest,
        evidence=evidence,
    )


def _arguments() -> Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("command", choices=("create", "verify"))
    _ = parser.add_argument("--todo6a", type=Path, required=True)
    _ = parser.add_argument("--todo6b", type=Path, required=True)
    _ = parser.add_argument("--verify-local", type=Path, required=True)
    _ = parser.add_argument("--scan-release", type=Path, required=True)
    _ = parser.add_argument("--runtime-audit", type=Path, required=True)
    _ = parser.add_argument("--output", type=Path, required=True)
    arguments = Arguments()
    _ = parser.parse_args(namespace=arguments)
    return arguments


def main() -> None:
    """Write an exclusive canonical receipt or compare it byte-for-byte."""
    arguments = _arguments()
    receipt = build_receipt(arguments)
    payload = (receipt.model_dump_json(indent=2) + "\n").encode()
    if arguments.command == "verify":
        if _regular_bytes(arguments.output) != payload:
            reason = "canonical_evidence_mismatch"
            raise SystemExit(reason)
        return
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        arguments.output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                reason = "canonical evidence write made no progress"
                raise OSError(reason)
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
