import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field

from . import browser_prod_gate
from .browser_observability import PageAudit

_ROOT = Path(__file__).resolve().parents[2]


class _PackageContract(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    name: Literal["nvidia-build-lb-qa"]
    private: Literal[True]
    dev_dependencies: dict[str, str] = Field(alias="devDependencies")


class _LockedPackage(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow", frozen=True)

    dev_dependencies: dict[str, str] | None = Field(default=None, alias="devDependencies")


class _PackageLock(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow", frozen=True)

    lockfile_version: Literal[3] = Field(alias="lockfileVersion")
    packages: dict[str, _LockedPackage]


def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def test_browser_prod_gate_has_closed_entrypoint_and_dependency_surface() -> None:
    script_path = _ROOT / "scripts/qa/test-browser-prod.sh"
    assert script_path.is_file()
    assert os.access(script_path, os.X_OK)
    script = script_path.read_text(encoding="utf-8")

    assert "IMAGE_DIGEST" in script
    assert "POSTGRES_IMAGE_DIGEST" in script
    assert "SOURCE_MANIFEST" in script
    assert "EVIDENCE_DIR" in script
    assert "NBLB_QA_TASK_LABEL=todo6b-browser-prod" in script
    assert "trap cleanup EXIT" in script
    assert script.index("trap cleanup EXIT") < script.index("mktemp -d")
    assert "docker compose" in script
    assert "down --volumes --remove-orphans" in script
    assert 'mkdir -p "$(dirname "$EVIDENCE_DIR")"' in script
    assert 'exec 9<>"$claim_path"' in script
    assert "assert_no_symlink_components" in script
    assert "regular_file_no_symlink" in script
    assert "if ! flock -n 9; then" in script
    assert 'if ! mkdir "$EVIDENCE_DIR" 2>/dev/null; then' in script
    assert 'mkdir -p "$EVIDENCE_DIR/runs"' not in script
    assert 'mkdir "$EVIDENCE_DIR/runs"' in script
    assert "postgres_images=-1; cleanup_error=1" in script
    assert "temporary_postgres_image_count()" in script
    assert "remaining:{containers:0" not in script
    assert 'pgrep -f "$pattern" || true' not in script
    assert "tests.ui.browser_prod_gate" in script
    assert "tests.ui.lighthouse_gate" in script
    assert "env " not in script
    assert "printenv" not in script
    assert "sleep " not in script
    assert "--disable-web-security" not in script

    package = _PackageContract.model_validate_json(_text("package.json"))
    assert package.name == "nvidia-build-lb-qa"
    assert package.private
    assert package.dev_dependencies == {"lighthouse": "13.4.0"}
    lock = _PackageLock.model_validate_json(_text("package-lock.json"))
    assert lock.lockfile_version == 3
    assert lock.packages[""].dev_dependencies == {"lighthouse": "13.4.0"}


def test_browser_gate_accepts_only_default_or_fresh_final_browser_leaf() -> None:
    script_path = _ROOT / "scripts/qa/test-browser-prod.sh"
    accepted = (
        ".omo/evidence/task-6b-nvidia-build-lb",
        ".omo/evidence/final-20260715T123456Z-deadbeef/browser",
    )
    rejected = (
        ".omo/evidence/task-6b-release",
        ".omo/evidence/final-20260715T123456Z-deadbeef",
        ".omo/evidence/final-20260715T123456Z-deadbeef/verify",
        ".omo/evidence/final-20260715T123456Z-DEADBEEF/browser",
        ".omo/evidence/final-20260715T123456-deadbeef/browser",
        ".omo/evidence/final-20260715T123456Z-deadbeef/browser/nested",
    )

    for evidence in accepted:
        completed = subprocess.run(  # noqa: S603 - fixed repository gate entrypoint.
            [script_path],
            cwd=_ROOT,
            env=os.environ | {"EVIDENCE_DIR": evidence},
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 64
        assert completed.stderr == "invalid_image_digest\n"

    for evidence in rejected:
        completed = subprocess.run(  # noqa: S603 - fixed repository gate entrypoint.
            [script_path],
            cwd=_ROOT,
            env=os.environ | {"EVIDENCE_DIR": evidence},
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 64
        assert completed.stderr == "invalid_evidence_directory\n"


def test_browser_prod_gate_reports_secret_safe_exception_phases() -> None:
    audit = PageAudit()
    audit.runtime_exceptions.extend(("upstream_enable_401", "upstream_probe_503"))
    audit.page_errors.append("native-upstream_enable_401")

    with pytest.raises(AssertionError) as raised:
        browser_prod_gate.assert_expected_errors(audit)

    assert str(raised.value) == (
        "production browser observed a runtime or page exception: "
        "runtime_phases=('upstream_enable_401', 'upstream_probe_503'); "
        "page_phases=('native-upstream_enable_401',)"
    )


def test_browser_prod_gate_is_snapshot_bound_and_preserves_observed_cleanup() -> None:
    script = _text("scripts/qa/test-browser-prod.sh")

    assert "scripts.qa.source_snapshot" in script
    assert 'PYTHONPATH="$QA_SOURCE_DIR/src:$QA_SOURCE_DIR"' in script
    assert '.venv/bin/python" -P "$@"' in script
    assert 'uv run --project "$ROOT"' not in script
    assert 'npm ci --prefix "$NODE_RUNTIME_DIR"' in script
    assert 'cp "$QA_SOURCE_DIR/package.json"' in script
    assert "NBLB_LIGHTHOUSE_PATH" in script
    assert 'cmp "$EVIDENCE_DIR/source-manifest.json" "$after"' in script
    assert '"$QA_SOURCE_DIR/compose.qa.yml"' in script
    assert "docker buildx build" not in script
    assert "NBLB_POSTGRES_IMAGE=$POSTGRES_IMAGE_DIGEST" in script
    assert 'cmp "$SOURCE_MANIFEST" "$CLIENT_DIR/source-manifest-current.json"' in script
    assert "process-baseline.json" in script
    assert "process_baseline_sha256" in script
    assert "fresh_cleanup_sha256" in _text("tests/ui/browser_prod_verify.py")
    assert "run_artifact_sha256" in _text("tests/ui/browser_prod_verify.py")
    assert "cleanup-fresh.json" in script
    assert "validate_fresh_cleanup" in script
    assert "atomic_open" in script
    assert 'mv -T -- "$ATOMIC_TEMPORARY" "$ATOMIC_DESTINATION"' in script
    assert "browser_temporary_directory_count()" in script
    assert "browser_temporary_directories" in script
    assert "collect_process_identities" in script
    assert 'identity=$(process_identity "$pid")' in script
    assert script.count("qa_python -m tests.ui.browser_prod_verify") == 2


def test_browser_prod_process_observer_distinguishes_absence_error_and_pid_reuse() -> None:
    bash = shutil.which("bash")
    assert bash is not None
    script = _text("scripts/qa/test-browser-prod.sh")
    observer = (
        "process_identity() {"
        + script.split("process_identity() {", maxsplit=1)[1].split("\ncleanup() {", maxsplit=1)[0]
    )
    assertions = r"""
pgrep() { return 1; }
[ "$(new_process_count ignored '')" -eq 0 ]
pgrep() { return 2; }
set +e
new_process_count ignored '' >/dev/null
observer_status=$?
set -e
[ "$observer_status" -eq 2 ]
pgrep() { printf '%s\n' "$$"; }
baseline=$(process_identity "$$")
[ "$(new_process_count ignored "$baseline")" -eq 0 ]
[ "$(new_process_count ignored "${baseline%:*}:0")" -eq 1 ]
"""

    completed = subprocess.run(  # noqa: S603
        [bash, "-c", "set -Eeuo pipefail\n" + observer + assertions],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_browser_prod_cleanup_validator_and_atomic_writer_fail_closed(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    assert bash is not None
    script = _text("scripts/qa/test-browser-prod.sh")
    atomic = (
        "atomic_open() {"
        + script.split("atomic_open() {", maxsplit=1)[1].split("\ncleanup() {", maxsplit=1)[0]
    )
    validator = (
        "validate_fresh_cleanup() {"
        + script.split("validate_fresh_cleanup() {", maxsplit=1)[1].split(
            "\nassert_preflight_clean() {", maxsplit=1
        )[0]
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
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
    _ = (evidence / "cleanup-fresh.json").write_text(json.dumps(receipt), encoding="utf-8")
    outside = tmp_path / "outside.json"
    _ = outside.write_text("outside\n", encoding="utf-8")
    destination = evidence / "cleanup-resume.json"
    _ = destination.symlink_to(outside)
    assertions = r"""
validate_fresh_cleanup
atomic_open "$EVIDENCE_DIR/cleanup-resume.json"
printf '%s\n' replacement >&"$ATOMIC_FD"
atomic_commit
[ ! -L "$EVIDENCE_DIR/cleanup-resume.json" ]
[ "$(cat "$EVIDENCE_DIR/cleanup-resume.json")" = replacement ]
[ "$(cat "$OUTSIDE")" = outside ]
atomic_open "$EVIDENCE_DIR/cleanup-fresh-mirror-failed.json"
printf '%s\n' candidate >&"$ATOMIC_FD"
cp() { return 1; }
set +e
commit_cleanup_receipt
mirror_status=$?
set -e
[ "$mirror_status" -ne 0 ]
[ ! -e "$EVIDENCE_DIR/cleanup-fresh-mirror-failed.json" ]
"""
    environment = os.environ.copy()
    environment.update(
        {
            "BASE_RUN_ID": "observer-test",
            "EVIDENCE_DIR": str(evidence),
            "OUTSIDE": str(outside),
        }
    )
    completed = subprocess.run(  # noqa: S603
        [bash, "-c", "set -Eeuo pipefail\n" + atomic + validator + assertions],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr

    receipt["status"] = "FAIL"
    _ = (evidence / "cleanup-fresh.json").write_text(json.dumps(receipt), encoding="utf-8")
    rejected = subprocess.run(  # noqa: S603
        [bash, "-c", "set -Eeuo pipefail\n" + validator + "validate_fresh_cleanup\n"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert rejected.returncode != 0


def test_browser_prod_gate_uses_task_label_and_fixed_managed_browser_contract() -> None:
    compose = _text("compose.qa.yml")
    gate = _text("tests/ui/browser_prod_gate.py")
    lighthouse = _text("tests/ui/lighthouse_gate.py")

    assert compose.count("${NBLB_QA_TASK_LABEL:-todo6a-candidate}") == 8
    assert "chromium_revision=1228" in gate
    assert "capture_public_surfaces" in gate
    assert "start_native_headless_context" in gate
    assert "credentialState()" in gate
    assert "headers" not in lighthouse
    assert "response.body" not in lighthouse
    assert "--chrome-path" in lighthouse
    assert '"--chrome-flags=--headless=new --no-sandbox"' in lighthouse
    assert "--only-categories=performance,accessibility,best-practices,seo" in lighthouse
    assert "median" in lighthouse
