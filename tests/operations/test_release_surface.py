"""Static release, migration, workflow, and shell safety contracts."""

import re
import shutil
import subprocess
from pathlib import Path

from scripts.qa.scan_sensitive_patterns import matching_paths

_ROOT = Path(__file__).resolve().parents[2]
_ACTION_PIN = re.compile(r"^\s*-?\s*uses:\s*[^\s@]+@[0-9a-f]{40}\s*(?:#.*)?$")


def _text(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def test_migrations_take_and_release_a_dedicated_session_advisory_lock() -> None:
    environment = _text("migrations/env.py")
    locked_runner = environment[
        environment.index("def _run_migrations(") : environment.index(
            "async def _run_migrations_online"
        )
    ]

    assert "pg_advisory_lock" in environment
    assert "pg_advisory_unlock" in environment
    assert "_MIGRATION_LOCK_KEY_ONE" in environment
    assert "_MIGRATION_LOCK_KEY_TWO" in environment
    assert "finally:" in environment
    assert locked_runner.index("pg_advisory_lock") < locked_runner.index("context.run_migrations")
    assert locked_runner.index("context.run_migrations") < locked_runner.index("pg_advisory_unlock")


def test_backup_and_restore_scripts_fail_closed_on_custody_or_target_drift() -> None:
    backup = _text("scripts/ops/backup.sh")
    restore = _text("scripts/ops/restore.sh")
    state = _text("scripts/ops/database-state.sh")

    for script in (backup, restore, state):
        assert script.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
        assert "eval " not in script
        assert "printenv" not in script
        assert "docker inspect" in script

    assert "flock -n" in backup
    assert "source_app_must_be_stopped" in backup
    assert "vault_master_key" in backup
    assert "database.dump" in backup
    assert "nvidia-build-lb.backup-source" in backup
    assert "nvidia-build-lb.restore-isolated" in restore
    assert "restore_target_not_isolated" in restore
    assert "target_database_not_empty" in restore
    assert "pg_restore" in restore
    assert "downstream_digest_sha256" in state
    assert "encode(token_digest,'hex')" in state


def test_local_verifier_and_release_scanner_own_evidence_and_cleanup() -> None:
    verify = _text("scripts/qa/verify-local.sh")
    scan = _text("scripts/qa/scan-release.sh")

    for script in (verify, scan):
        assert script.startswith("#!/usr/bin/env bash\nset -Eeuo pipefail\n")
        assert "manual-qa.json" in script
        assert "adversarial.json" in script
        assert "cleanup.json" in script
        assert "flock -n" in script
        assert "trap cleanup EXIT" in script
        assert "IMAGE_DIGEST" in script
        assert "sha256:" in script

    assert "127.0.0.1:2455/health" in verify
    assert "scripts/ops/backup.sh" in verify
    assert "scripts/ops/restore.sh" in verify
    assert "alembic_version" in verify
    assert "migration_failed" in verify
    assert "pip-audit" in scan
    assert "gitleaks" in scan
    assert "trivy" in scan
    assert scan.count('--user "$(id -u):$(id -g)"') >= 5
    assert "gitleaks_source_incomplete" in scan
    assert "docker history --no-trunc" in scan
    assert "mapfile -t python_base_images" in scan
    assert "candidate_public_gpg_key_drift" in scan
    assert "candidate_public_gpg_history_drift" in scan
    assert scan.count("verified-pinned-base-public-key") == 2
    assert "pinned_base_public_gpg_key_verified" in scan
    assert "uses:" in scan


def test_production_compose_and_operations_docs_keep_the_release_boundary_closed() -> None:
    compose = _text("compose.yml")

    assert "${NBLB_APP_IMAGE:?immutable application image digest required}" in compose
    assert "${NBLB_POSTGRES_IMAGE:?immutable PostgreSQL image digest required}" in compose
    assert "${NBLB_BIND_ADDRESS:-127.0.0.1}:${NBLB_PORT:-2456}:2456" in compose
    assert "nvidia-build-lb.restore-isolated: ${NBLB_RESTORE_ISOLATED:-false}" in compose
    assert "nvidia-build-lb.backup-source: ${NBLB_BACKUP_SOURCE:-true}" in compose
    assert compose.count('com.centurylinklabs.watchtower.enable: "false"') == 3
    assert "internal: true" in compose
    assert "latest" not in compose.lower()

    backup = _text("docs/BACKUP_RESTORE.md")
    security = _text("docs/SECURITY.md")
    rollback = _text("docs/ROLLBACK.md")
    runbook = _text("docs/RUNBOOK.md")
    assert "restore-isolated=true" in backup
    assert "Never restore over the live volume" in backup
    assert "Do not rotate `vault_master_key` in isolation" in runbook
    assert "full 40-hex commit pins" in security
    assert "Do not run Alembic downgrade" in rollback
    trivy_exceptions = [
        line for line in _text(".trivyignore").splitlines() if line and not line.startswith("#")
    ]
    assert trivy_exceptions == ["DS-0002"]


def test_workflows_use_only_full_sha_actions_and_minimum_job_permissions() -> None:
    workflows = sorted((_ROOT / ".github/workflows").glob("*.yml"))
    assert {path.name for path in workflows} == {"ci.yml", "publish-ghcr.yml"}

    for workflow in workflows:
        text = workflow.read_text(encoding="utf-8")
        action_lines = [line for line in text.splitlines() if "uses:" in line]
        assert action_lines
        assert all(_ACTION_PIN.fullmatch(line) is not None for line in action_lines)
        assert "pull_request_target" not in text
        assert "permissions:\n  contents: read" in text

    publish = _text(".github/workflows/publish-ghcr.yml")
    assert "packages: write" in publish
    assert "id-token: write" not in publish
    assert "latest" not in publish.lower()


def test_product_pattern_scan_allows_only_named_synthetic_examples(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    _ = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [git, "init", "-q"], cwd=tmp_path, check=True
    )
    safe = tmp_path / "safe.txt"
    example_token = "nblb_ds_" + ("a" * 64)
    _ = safe.write_text(
        f"nvapi-synthetic-fixture-key\n{example_token}\n",
        encoding="utf-8",
    )
    assert matching_paths(tmp_path) == ()

    unsafe = tmp_path / "unsafe.txt"
    _ = unsafe.write_text("nblb_ds_" + ("0" * 64) + "\n", encoding="utf-8")
    assert matching_paths(tmp_path) == ("unsafe.txt",)
