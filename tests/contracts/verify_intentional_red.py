"""Fail-closed verifier for the checked-in intentional-red manifest."""

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal, Never

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

_ROOT = Path(__file__).resolve().parents[2]
_EXPECTED_PATH = Path(__file__).with_name("intentional_red_expected.json")
_RECORDER_MODULE = "contracts._red_recorder"
_NODE_PATTERN = re.compile(
    r"^tests/contracts/test_[A-Za-z0-9_]+\.py::[A-Za-z0-9_]+(?:\[[^\r\n]+\])?$"
)


class ExpectedResult(BaseModel):
    """One predeclared semantic failure."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=300)
    failure_class: Literal["AssertionError"]


class ExpectedManifest(BaseModel):
    """Checked-in expected suite identity."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    suite: Literal["tests/contracts"]
    verified_green: tuple[str, ...]
    expected: tuple[ExpectedResult, ...]


class RecordedResult(BaseModel):
    """One safe pytest phase result."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1, max_length=300)
    phase: Literal["setup", "call", "teardown"]
    outcome: Literal["passed", "failed", "skipped"]
    failure_class: str | None
    xfail: bool


class RecorderPayload(BaseModel):
    """Complete private-recorder payload without failure text."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    collected: tuple[str, ...]
    collection_errors: int = Field(ge=0)
    reports: tuple[RecordedResult, ...]
    exit_status: int


@dataclass(frozen=True, slots=True)
class _ObservedSuite:
    collection: RecorderPayload
    green: RecorderPayload
    red: RecorderPayload


@dataclass(frozen=True, slots=True)
class _VerifiedRed:
    expected: tuple[ExpectedResult, ...]
    observed: tuple[ExpectedResult, ...]
    manifest_hash: str
    verified_green: tuple[str, ...]


class VerificationError(Exception):
    """Stable verifier failure that carries no pytest output."""


def _fail(code: str) -> Never:
    raise VerificationError(code)


def _validate_nodes(nodes: Sequence[str], *, label: str) -> None:
    if len(nodes) != len(set(nodes)):
        _fail(f"{label}.duplicate_node")
    if not nodes or any(_NODE_PATTERN.fullmatch(node) is None for node in nodes):
        _fail(f"{label}.malformed_node")


def _load_expected() -> ExpectedManifest:
    try:
        manifest = ExpectedManifest.model_validate_json(_EXPECTED_PATH.read_bytes())
    except (OSError, ValidationError):
        _fail("expected.invalid")
    nodes = tuple(item.node_id for item in manifest.expected)
    _validate_nodes(nodes, label="expected")
    _validate_nodes(manifest.verified_green, label="verified_green")
    if set(nodes) & set(manifest.verified_green):
        _fail("expected.overlapping_nodes")
    return manifest


def _run_pytest(arguments: list[str], *, environment: dict[str, str] | None = None) -> int:
    command = [sys.executable, "-m", "pytest", *arguments]
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and fixed verifier arguments.
        command,
        cwd=_ROOT,
        env=environment,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode


def _record(report_path: Path, arguments: list[str], expected_exit: int) -> RecorderPayload:
    report_path.unlink(missing_ok=True)
    report_path.with_suffix(f"{report_path.suffix}.tmp").unlink(missing_ok=True)
    environment = dict(os.environ)
    environment["NBLB_RED_REPORT_PATH"] = str(report_path)
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(_ROOT / "tests") + (
        f"{os.pathsep}{existing_python_path}" if existing_python_path else ""
    )
    run_status = _run_pytest([*arguments, "-q", "-p", _RECORDER_MODULE], environment=environment)
    if run_status != expected_exit:
        _fail("pytest.unexpected_exit")
    try:
        payload = RecorderPayload.model_validate_json(report_path.read_bytes())
    except (OSError, ValidationError):
        _fail("recorder.invalid")
    if payload.collection_errors != 0 or payload.exit_status != expected_exit:
        _fail("recorder.collection_or_exit_mismatch")
    return payload


def _observe(report_path: Path, expected: ExpectedManifest) -> _ObservedSuite:
    collection = _record(
        report_path.with_name("nblb-intentional-red-collection.json"),
        ["tests/contracts", "--collect-only"],
        0,
    )
    green = _record(
        report_path.with_name("nblb-intentional-red-green.json"),
        list(expected.verified_green),
        0,
    )
    red = _record(report_path, [item.node_id for item in expected.expected], 1)
    return _ObservedSuite(collection=collection, green=green, red=red)


def _verify(
    expected: ExpectedManifest,
    observed: _ObservedSuite,
) -> _VerifiedRed:
    expected_sorted = tuple(sorted(expected.expected, key=lambda item: item.node_id))
    expected_nodes = tuple(item.node_id for item in expected_sorted)
    complete_nodes = tuple(sorted((*expected_nodes, *expected.verified_green)))
    _validate_nodes(observed.collection.collected, label="collected")
    if (
        tuple(sorted(observed.collection.collected)) != complete_nodes
        or observed.collection.reports
    ):
        _fail("collection.node_mismatch")
    if tuple(sorted(observed.green.collected)) != tuple(sorted(expected.verified_green)):
        _fail("green.node_mismatch")
    if any(
        report.phase != "call"
        or report.outcome != "passed"
        or report.xfail
        or report.failure_class is not None
        for report in observed.green.reports
    ):
        _fail("green.unexpected_result")
    report_nodes = tuple(report.node_id for report in observed.red.reports)
    _validate_nodes(report_nodes, label="observed")
    if tuple(sorted(observed.red.collected)) != expected_nodes:
        _fail("red.node_mismatch")
    if any(report.phase != "call" for report in observed.red.reports):
        _fail("observed.fixture_or_teardown_error")
    if any(report.outcome != "failed" for report in observed.red.reports):
        _fail("observed.pass_skip_or_nonfailure")
    if any(report.xfail for report in observed.red.reports):
        _fail("observed.xfail_or_xpass")
    if any(report.failure_class != "AssertionError" for report in observed.red.reports):
        _fail("observed.unexpected_failure_class")
    observed_sorted = tuple(
        ExpectedResult(node_id=report.node_id, failure_class="AssertionError")
        for report in sorted(observed.red.reports, key=lambda item: item.node_id)
    )
    if observed_sorted != expected_sorted:
        _fail("observed.result_mismatch")
    canonical = json.dumps(
        [item.model_dump(mode="json") for item in expected_sorted],
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return _VerifiedRed(
        expected=expected_sorted,
        observed=observed_sorted,
        manifest_hash=hashlib.sha256(canonical).hexdigest(),
        verified_green=expected.verified_green,
    )


def _write_evidence(
    evidence_dir: Path,
    verification: _VerifiedRed,
) -> None:
    target = evidence_dir / "intentional-red.json"
    temporary = evidence_dir / ".intentional-red.json.tmp"
    counts = Counter(item.failure_class for item in verification.observed)
    verified_green_json: list[JsonValue] = []
    verified_green_json.extend(sorted(verification.verified_green))
    document: dict[str, JsonValue] = {
        "schema_version": 2,
        "suite": "tests/contracts",
        "status": "verified-intentional-red",
        "expected": [item.model_dump(mode="json") for item in verification.expected],
        "observed": [item.model_dump(mode="json") for item in verification.observed],
        "expected_count": len(verification.expected),
        "observed_count": len(verification.observed),
        "failure_classes": dict(sorted(counts.items())),
        "manifest_sha256": verification.manifest_hash,
        "verified_green": verified_green_json,
        "verified_green_count": len(verification.verified_green),
        "collection_exit_status": 0,
        "red_exit_status": 1,
        "rejected": {
            "collection_errors": 0,
            "fixture_or_bootstrap_errors": 0,
            "passed": 0,
            "skipped": 0,
            "xfail_or_xpass": 0,
            "malformed": 0,
            "extra": 0,
            "missing": 0,
            "duplicate": 0,
        },
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    try:
        _ = temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        _ = temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def _evidence_dir(arguments: Sequence[str]) -> Path:
    if len(arguments) != 2 or arguments[0] != "--evidence-dir" or not arguments[1]:
        _fail("arguments.invalid")
    return Path(arguments[1])


def main(arguments: Sequence[str] | None = None) -> int:
    """Collect, observe, compare, and atomically persist safe RED evidence."""
    report_path = _ROOT / ".pytest_cache" / "nblb-intentional-red-report.json"
    try:
        expected_manifest = _load_expected()
        destination = _evidence_dir(sys.argv[1:] if arguments is None else arguments)
        observed = _observe(report_path, expected_manifest)
        verification = _verify(expected_manifest, observed)
        _write_evidence(destination, verification)
    except VerificationError as error:
        _ = sys.stderr.write(f"{error}\n")
        return 1
    finally:
        for path in (
            report_path,
            report_path.with_name("nblb-intentional-red-collection.json"),
            report_path.with_name("nblb-intentional-red-green.json"),
        ):
            path.unlink(missing_ok=True)
            path.with_suffix(f"{path.suffix}.tmp").unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
