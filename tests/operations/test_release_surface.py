"""Static release, migration, workflow, and shell safety contracts."""

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest
from scripts.qa.scan_sensitive_patterns import matching_paths

_ROOT = Path(__file__).resolve().parents[2]
_ACTION_PIN = re.compile(r"^\s*-?\s*uses:\s*[^\s@]+@[0-9a-f]{40}\s*(?:#.*)?$")
_RESTORE_DATABASE = b"PGDMP\x00immutable-snapshot"
_RESTORE_KEY = b"k" * 32
_RESTORE_MANIFEST = b'{"fixture":"immutable-snapshot"}\n'
_SWAPPED_DATABASE = b"PGDMP\x00source-swapped"
_SWAPPED_KEY = b"z" * 32
_SWAPPED_MANIFEST = b'{"fixture":"source-swapped"}\n'


@dataclass(frozen=True, slots=True)
class _RestoreExecution:
    completed: subprocess.CompletedProcess[str]
    database_source: Path
    key_source: Path
    manifest_source: Path
    target_key: Path
    stage_path: Path


@dataclass(frozen=True, slots=True)
class _BackupExecution:
    completed: subprocess.CompletedProcess[str]
    manifest: Path
    database_state: Path


_FAKE_BACKUP_DOCKER = r"""#!/usr/bin/env python3
import os
import shutil
import sys
import time
from pathlib import Path

ARGS = sys.argv[1:]


def fail(reason):
    sys.stderr.write(reason + "\n")
    raise SystemExit(97)


def mounts():
    result = {}
    for index, argument in enumerate(ARGS[:-1]):
        if argument != "--mount":
            continue
        fields = {}
        for item in ARGS[index + 1].split(","):
            key, separator, value = item.partition("=")
            if separator:
                fields[key] = value
        result[fields["target"]] = Path(fields["source"])
    return result


if not ARGS:
    fail("synthetic_missing_command")

if ARGS[0] == "inspect":
    template = ARGS[ARGS.index("--format") + 1]
    container = ARGS[-1]
    database = container.startswith("b")
    if "State.Running" in template:
        print("true" if database else "false")
    elif "backup-source" in template:
        print("true" if database else "false")
    elif "nvidia-build-lb.component" in template:
        print("database" if database else "gateway")
    elif "com.docker.compose.service" in template:
        print("db" if database else "app")
    elif "com.docker.compose.project" in template:
        print("backup-contract-test")
    else:
        fail("synthetic_unknown_inspect")
    raise SystemExit(0)

if ARGS[0] == "exec":
    if "pg_dump" in ARGS:
        sys.stdout.buffer.write(b"PGDMP\x00backup-fixture")
        raise SystemExit(0)
    if "--command" not in ARGS:
        fail("synthetic_unknown_exec")
    query = ARGS[ARGS.index("--command") + 1]
    if "SELECT version_num" in query:
        print("0004_vault_key_verifier")
    elif "information_schema.columns" in query and "admin_ledger_state_pkey" in query:
        print("0\t1")
    elif "encode(verifier_salt" in query:
        print(("1" * 64) + "\t" + ("2" * 64))
    elif "jsonb_agg" in query:
        print("[]")
    elif "SELECT count(*) FROM downstream_tokens" in query:
        print("0")
    elif "encode(token_digest" not in query:
        fail("synthetic_unknown_query")
    raise SystemExit(0)

if ARGS[0] != "run":
    fail("synthetic_unknown_command")

bound = mounts()
shell_command = ARGS[ARGS.index("-c") + 1] if "-c" in ARGS else ""

if os.environ.get("NBLB_FAKE_FAIL_AT") == "after-lock":
    raise SystemExit(42)

if 'mkdir "/output/$1"' in shell_command:
    if signal_name := os.environ.get("NBLB_FAKE_HOLD_SIGNAL"):
        signal = Path(signal_name)
        if not signal.exists():
            signal.touch()
            release = Path(os.environ["NBLB_FAKE_HOLD_RELEASE"])
            deadline = time.monotonic() + 10
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not release.exists():
                fail("synthetic_hold_timeout")
    destination = bound["/output"] / ARGS[-1]
    destination.mkdir(mode=0o700)
    raise SystemExit(0)

if "cp /source/vault_master_key" in shell_command:
    shutil.copyfile(bound["/source/vault_master_key"], bound["/output"] / "vault_master_key")
    (bound["/output"] / "vault_master_key").chmod(0o600)
    raise SystemExit(0)

if "cat > /output/database-state.json" in shell_command:
    state = bound["/output"] / "database-state.json"
    state.write_bytes(sys.stdin.buffer.read())
    state.chmod(0o600)
    raise SystemExit(0)

if "verify-vault-key" in ARGS:
    if not (bound["/key"] / "vault_master_key").is_file():
        fail("synthetic_key_missing")
    if not bound["/state/database-state.json"].is_file():
        fail("synthetic_state_missing")
    raise SystemExit(0)

if "cat > /output/.database.dump.tmp" in shell_command:
    destination = bound["/output"] / "database.dump"
    destination.write_bytes(sys.stdin.buffer.read())
    destination.chmod(0o600)
    raise SystemExit(0)

if "nvidia_build_lb.backup_contract" in ARGS and "create" in ARGS:
    manifest = bound["/manifest"] / "manifest.json"
    manifest.write_text('{"schema_version":2,"status":"PASS"}\n', encoding="utf-8")
    manifest.chmod(0o600)
    print('{"schema_version":2,"status":"PASS"}')
    raise SystemExit(0)

if "rm -f /output/database-state.json" in shell_command:
    if os.environ.get("NBLB_FAKE_FAIL_AT") == "state-cleanup":
        raise SystemExit(42)
    (bound["/output"] / "database-state.json").unlink(missing_ok=True)
    raise SystemExit(0)

fail("synthetic_unknown_run")
"""


_FAKE_RESTORE_DOCKER = r"""#!/usr/bin/env python3
import hashlib
import os
import shutil
import sys
import time
from pathlib import Path

ARGS = sys.argv[1:]


def fail(reason):
    sys.stderr.write(reason + "\n")
    raise SystemExit(97)


def mounts():
    result = {}
    for index, argument in enumerate(ARGS[:-1]):
        if argument != "--mount":
            continue
        fields = {}
        for item in ARGS[index + 1].split(","):
            key, separator, value = item.partition("=")
            if separator:
                fields[key] = value
        result[fields["target"]] = Path(fields["source"])
    return result


def require_hash(path, variable):
    observed = hashlib.sha256(path.read_bytes()).hexdigest()
    if observed != os.environ[variable]:
        fail("synthetic_artifact_drift")


def verify_stage(stage):
    require_hash(stage / "database.dump", "NBLB_FAKE_DATABASE_SHA256")
    require_hash(stage / "vault_master_key", "NBLB_FAKE_KEY_SHA256")
    require_hash(stage / "manifest.json", "NBLB_FAKE_MANIFEST_SHA256")


if not ARGS:
    fail("synthetic_missing_command")

if ARGS[0] == "inspect":
    template = ARGS[ARGS.index("--format") + 1]
    if "State.Running" in template:
        print("true")
    elif "restore-isolated" in template:
        print("true")
    elif "nvidia-build-lb.component" in template:
        print("database")
    elif "com.docker.compose.service" in template:
        print("db")
    elif "com.docker.compose.project" in template:
        print("restore-contract-test")
    else:
        fail("synthetic_unknown_inspect")
    raise SystemExit(0)

if ARGS[0] == "exec":
    if "pg_restore" in ARGS:
        payload = sys.stdin.buffer.read()
        observed = hashlib.sha256(payload).hexdigest()
        if observed != os.environ["NBLB_FAKE_DATABASE_SHA256"]:
            fail("synthetic_restore_received_swapped_dump")
        raise SystemExit(0)
    if "--command" not in ARGS:
        fail("synthetic_unknown_exec")
    query = ARGS[ARGS.index("--command") + 1]
    if "user_schemas" in query:
        print("0")
    elif "SELECT version_num" in query:
        print("0004_vault_key_verifier")
    elif "information_schema.columns" in query and "admin_ledger_state_pkey" in query:
        print("0\t1")
    elif "encode(verifier_salt" in query:
        print(("1" * 64) + "\t" + ("2" * 64))
    elif "jsonb_agg" in query:
        print("[]")
    elif "SELECT count(*) FROM downstream_tokens" in query:
        print("0")
    elif "encode(token_digest" not in query:
        fail("synthetic_unknown_query")
    raise SystemExit(0)

if ARGS[0] != "run":
    fail("synthetic_unknown_command")

bound = mounts()
shell_command = ARGS[ARGS.index("-c") + 1] if "-c" in ARGS else ""

if os.environ.get("NBLB_FAKE_FAIL_AT") == "after-lock":
    raise SystemExit(42)

if "rm -f /target/.vault_master_key.tmp /target/vault_master_key" in shell_command:
    marker = bound["/target"] / ".nblb-restore-install"
    marker_id = ARGS[-2]
    install_owned = ARGS[-1] == "1"
    if install_owned or (
        marker.is_file() and marker.read_text(encoding="utf-8").strip() == marker_id
    ):
        (bound["/target"] / ".vault_master_key.tmp").unlink(missing_ok=True)
        (bound["/target"] / "vault_master_key").unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
    raise SystemExit(0)

if "rm -f /state/database-state.json" in shell_command:
    if os.environ.get("NBLB_FAKE_FAIL_AT") == "state-cleanup":
        raise SystemExit(42)
    (bound["/state"] / "database-state.json").unlink(missing_ok=True)
    raise SystemExit(0)

if "rm -f /stage/database.dump" in shell_command:
    if os.environ.get("NBLB_FAKE_FAIL_AT") == "stage-cleanup":
        raise SystemExit(42)
    stage = bound["/stage"]
    for name in (
        "database.dump",
        "vault_master_key",
        "manifest.json",
        ".database.dump.tmp",
        ".vault_master_key.tmp",
        ".manifest.json.tmp",
    ):
        (stage / name).unlink(missing_ok=True)
    raise SystemExit(0)

source_targets = {"/database-root", "/key-root", "/manifest-root", "/stage"}
if source_targets <= bound.keys():
    if signal_name := os.environ.get("NBLB_FAKE_HOLD_SIGNAL"):
        signal = Path(signal_name)
        if not signal.exists():
            signal.touch()
            release = Path(os.environ["NBLB_FAKE_HOLD_RELEASE"])
            deadline = time.monotonic() + 10
            while not release.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if not release.exists():
                fail("synthetic_hold_timeout")
    backup_id = os.environ["NBLB_FAKE_BACKUP_ID"]
    database = bound["/database-root"] / backup_id / "database.dump"
    key = bound["/key-root"] / backup_id / "vault_master_key"
    manifest = bound["/manifest-root"] / backup_id / "manifest.json"
    stage = bound["/stage"]
    shutil.copyfile(database, stage / "database.dump")
    shutil.copyfile(key, stage / "vault_master_key")
    shutil.copyfile(manifest, stage / "manifest.json")
    Path(os.environ["NBLB_FAKE_STAGE_PATH_FILE"]).write_text(str(stage))
    database.write_bytes(b"PGDMP\x00source-swapped")
    key.write_bytes(b"z" * 32)
    manifest.write_bytes(b'{"fixture":"source-swapped"}\n')
    for source in (database, key, manifest):
        source.chmod(0o600)
    raise SystemExit(0)

if "cp /stage/vault_master_key" in shell_command:
    stage = bound["/stage"]
    target = bound["/target"] / "vault_master_key"
    marker = bound["/target"] / ".nblb-restore-install"
    if target.exists() or marker.exists():
        raise SystemExit(42)
    marker.write_text(ARGS[-1] + "\n", encoding="utf-8")
    marker.chmod(0o600)
    shutil.copyfile(stage / "vault_master_key", target)
    target.chmod(0o600)
    if os.environ.get("NBLB_FAKE_FAIL_AT") == "install-after-mv":
        raise SystemExit(42)
    raise SystemExit(0)

if "cat > /state/database-state.json" in shell_command:
    state_file = bound["/state"] / "database-state.json"
    state_file.write_bytes(sys.stdin.buffer.read())
    state_file.chmod(0o600)
    raise SystemExit(0)

if "verify-vault-key" in ARGS:
    if os.environ.get("NBLB_FAKE_FAIL_AT") == "verify-vault-key":
        raise SystemExit(42)
    require_hash(bound["/target"] / "vault_master_key", "NBLB_FAKE_KEY_SHA256")
    if not bound["/state/database-state.json"].is_file():
        fail("synthetic_state_missing")
    raise SystemExit(0)

if "compare-state" in ARGS:
    require_hash(bound["/stage"] / "manifest.json", "NBLB_FAKE_MANIFEST_SHA256")
    print('{"schema_version":2,"status":"PASS"}')
    raise SystemExit(0)

if 'test -f "$marker"' in shell_command and 'rm -f "$marker"' in shell_command:
    marker = bound["/target"] / ".nblb-restore-install"
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != ARGS[-1]:
        fail("synthetic_install_marker_mismatch")
    if os.environ.get("NBLB_FAKE_FAIL_AT") == "marker-commit":
        raise SystemExit(42)
    marker.unlink()
    if os.environ.get("NBLB_FAKE_FAIL_AT") == "marker-commit-after-unlink":
        raise SystemExit(42)
    raise SystemExit(0)

if "nvidia_build_lb.backup_contract" in ARGS and "verify" in ARGS:
    verify_stage(bound["/stage"])
    if "/target" in bound:
        require_hash(bound["/target"] / "vault_master_key", "NBLB_FAKE_KEY_SHA256")
    raise SystemExit(0)

if "/bin/cat" in ARGS:
    payload = bound["/stage"] / "database.dump"
    require_hash(payload, "NBLB_FAKE_DATABASE_SHA256")
    sys.stdout.buffer.write(payload.read_bytes())
    raise SystemExit(0)

fail("synthetic_unknown_run")
"""


_FAKE_RESTORE_JQ = r"""#!/usr/bin/env python3
import json
import sys

arguments = sys.argv[1:]
if any("cS" in argument for argument in arguments):
    _ = sys.stdin.read()
    print("[]")
elif "-er" in arguments:
    _ = sys.stdin.read()
    print("0")
elif "-n" in arguments:
    print(json.dumps({
        "schema_version": 2,
        "alembic_revision": "0004_vault_key_verifier",
        "vault_verifier_salt": "1" * 64,
        "vault_verifier_digest": "2" * 64,
        "upstream_count": 0,
        "upstream_identity_sha256": "3" * 64,
        "upstream_keys": [],
        "downstream_count": 0,
        "downstream_digest_sha256": "4" * 64,
    }, separators=(",", ":")))
else:
    raise SystemExit(2)
"""


def _text(relative: str) -> str:
    return (_ROOT / relative).read_text(encoding="utf-8")


def _run_backup_with_cleanup_failure(tmp_path: Path) -> _BackupExecution:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_jq = fake_bin / "jq"
    _ = fake_docker.write_text(_FAKE_BACKUP_DOCKER, encoding="utf-8")
    _ = fake_jq.write_text(_FAKE_RESTORE_JQ, encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_jq.chmod(0o755)

    database_root = tmp_path / "database"
    key_root = tmp_path / "key"
    manifest_root = tmp_path / "manifest"
    for root in (database_root, key_root, manifest_root):
        root.mkdir(mode=0o700)
    vault_key = tmp_path / "vault_master_key"
    _ = vault_key.write_bytes(_RESTORE_KEY)
    vault_key.chmod(0o600)
    backup_id = "backup-20260714t000000z"
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o700)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "NBLB_FAKE_FAIL_AT": "state-cleanup",
            "NBLB_OPERATION_LOCK_DIR": str(runtime_directory),
        }
    )
    completed = subprocess.run(  # noqa: S603 - fixed repository backup entrypoint.
        [
            _ROOT / "scripts/ops/backup.sh",
            "--db-container",
            "b" * 64,
            "--app-container",
            "c" * 64,
            "--helper-image",
            f"sha256:{'a' * 64}",
            "--vault-key-file",
            vault_key,
            "--database-root",
            database_root,
            "--key-root",
            key_root,
            "--manifest-root",
            manifest_root,
            "--backup-id",
            backup_id,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return _BackupExecution(
        completed=completed,
        manifest=manifest_root / backup_id / "manifest.json",
        database_state=manifest_root / backup_id / "database-state.json",
    )


def _run_restore_with_source_swap(
    tmp_path: Path,
    *,
    fail_at: str = "",
    preexisting_key: bytes | None = None,
) -> _RestoreExecution:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_jq = fake_bin / "jq"
    _ = fake_docker.write_text(_FAKE_RESTORE_DOCKER, encoding="utf-8")
    _ = fake_jq.write_text(_FAKE_RESTORE_JQ, encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_jq.chmod(0o755)

    backup_id = "backup-20260714t000000z"
    database_source = tmp_path / "database" / backup_id / "database.dump"
    key_source = tmp_path / "key" / backup_id / "vault_master_key"
    manifest_source = tmp_path / "manifest" / backup_id / "manifest.json"
    for source in (database_source, key_source, manifest_source):
        source.parent.mkdir(parents=True)
    _ = database_source.write_bytes(_RESTORE_DATABASE)
    _ = key_source.write_bytes(_RESTORE_KEY)
    _ = manifest_source.write_bytes(_RESTORE_MANIFEST)
    for source in (database_source, key_source, manifest_source):
        source.chmod(0o600)

    target_directory = tmp_path / "target"
    target_directory.mkdir(mode=0o700)
    target_key = target_directory / "vault_master_key"
    if preexisting_key is not None:
        _ = target_key.write_bytes(preexisting_key)
        target_key.chmod(0o600)
    stage_path_file = tmp_path / "stage-path"
    runtime_directory = tmp_path / "runtime"
    runtime_directory.mkdir(mode=0o700)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "NBLB_FAKE_BACKUP_ID": backup_id,
            "NBLB_FAKE_DATABASE_SHA256": sha256(_RESTORE_DATABASE).hexdigest(),
            "NBLB_FAKE_KEY_SHA256": sha256(_RESTORE_KEY).hexdigest(),
            "NBLB_FAKE_MANIFEST_SHA256": sha256(_RESTORE_MANIFEST).hexdigest(),
            "NBLB_FAKE_STAGE_PATH_FILE": str(stage_path_file),
            "NBLB_FAKE_FAIL_AT": fail_at,
            "NBLB_OPERATION_LOCK_DIR": str(runtime_directory),
        }
    )
    completed = subprocess.run(  # noqa: S603 - fixed repository restore entrypoint.
        [
            _ROOT / "scripts/ops/restore.sh",
            "--db-container",
            "b" * 64,
            "--helper-image",
            f"sha256:{'a' * 64}",
            "--database-directory",
            database_source.parent,
            "--key-directory",
            key_source.parent,
            "--manifest",
            manifest_source,
            "--target-secret-dir",
            target_directory,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return _RestoreExecution(
        completed=completed,
        database_source=database_source,
        key_source=key_source,
        manifest_source=manifest_source,
        target_key=target_key,
        stage_path=Path(stage_path_file.read_text(encoding="utf-8")),
    )


def _wait_for_path(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"timed out waiting for {path.name}"


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
    for script in (backup, restore):
        assert "${NBLB_OPERATION_LOCK_DIR:-/run/lock/nvidia-build-lb}" in script
        assert 'exec 9>>"$lock_file"' in script
        assert 'rm -f -- "$lock_file"' not in script
        assert 'rm -f -- "$LOCK_FILE"' not in script
    assert "source_app_must_be_stopped" in backup
    assert "vault_master_key" in backup
    assert "database.dump" in backup
    assert "nvidia-build-lb.backup-source" in backup
    assert "source_compose_identity_mismatch" in backup
    assert "verify-vault-key" in backup
    assert "vault_key_database_mismatch" in backup
    assert "nvidia-build-lb.restore-isolated" in restore
    assert "restore_target_not_isolated" in restore
    assert "target_database_not_empty" in restore
    assert "user_schemas" in restore
    assert "public_objects" in restore
    assert "pg_restore" in restore
    assert "downstream_digest_sha256" in state
    assert "vault_verifier_digest" in state
    assert "nvidia-build-lb.run" not in state
    assert "encode(token_digest,'hex')" in state


def test_database_state_requires_exact_0005_columns_constraints_and_indexes() -> None:
    state = _text("scripts/ops/database-state.sh")

    assert "pg_get_constraintdef(constraint_row.oid,true)" in state
    assert "CREATE INDEX ix_admin_events_request_type ON public.admin_events" in state
    assert "FOREIGN KEY (attempt_started_event_id, id)" in state
    assert "constraint_row.convalidated" in state
    assert "SELECT * FROM expected_columns EXCEPT SELECT * FROM observed_columns" in state
    assert "SELECT * FROM expected_constraints EXCEPT SELECT * FROM observed_constraints" in state
    assert "SELECT * FROM expected_indexes EXCEPT SELECT * FROM observed_indexes" in state
    assert '[ "$v3_exact" = 1 ]' in state
    assert '[ "$v3_absent" = 1 ]' in state
    assert "8:12:4:1" not in state


def test_compose_exposes_validated_ledger_capacity_settings() -> None:
    compose = _text("compose.yml")

    assert "NVIDIA_BUILD_LB_ADMIN_EVENT_MAX_ROWS: ${NBLB_ADMIN_EVENT_MAX_ROWS:-100000}" in compose
    assert (
        "NVIDIA_BUILD_LB_ADMIN_ATTEMPT_MAX_ROWS: ${NBLB_ADMIN_ATTEMPT_MAX_ROWS:-40000}" in compose
    )
    assert (
        "NVIDIA_BUILD_LB_ADMIN_LEDGER_PRUNE_BATCH_SIZE: ${NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE:-1000}"
        in compose
    )


def test_operator_backup_and_restore_examples_preserve_failure_status() -> None:
    guide = _text("docs/BACKUP_RESTORE.md")

    backup = guide[guide.index("## Quiesced backup") : guide.index("## Mandatory isolated")]
    assert "trap backup_exit EXIT" in backup
    assert "trap 'exit 129' HUP" in backup
    assert backup.index("APP_STOPPED=1") < backup.index(
        "scripts/ops/production-compose.sh stop app"
    )
    assert backup.rindex("BACKUP_STATUS=$?") < backup.rindex(
        '[ "$BACKUP_STATUS" -eq 0 ] || exit "$BACKUP_STATUS"'
    )
    assert backup.rindex('[ "$BACKUP_STATUS" -eq 0 ]') < backup.rindex("restart_source_app")
    runtime_probe = "scripts/operator_readiness_probe.py"
    assert backup.rindex("restart_source_app") < backup.index(runtime_probe)
    assert "docker exec" not in backup
    assert "BACKUP_RUNTIME_ATTEMPT" in backup
    assert "backup_restart_runtime_timeout" in backup
    assert backup.index(runtime_probe) < backup.rindex("trap - EXIT HUP INT TERM")
    assert backup.rindex("trap - EXIT HUP INT TERM") < backup.rindex(
        '''printf '%s\\n' "$BACKUP_RECEIPT"'''
    )

    restore = guide[guide.index("## Mandatory isolated restore drill") :]
    assert "trap restore_exit EXIT" in restore
    assert (
        'RESTORE_SECRET_DIR="/opt/nvidia-build-lb/restore-secrets-$RESTORE_ATTEMPT_ID"' in restore
    )
    assert "down --volumes --remove-orphans" in restore
    assert 'export BACKUP_ID="replace-with-verified-backup-id"' in restore
    assert restore.index("restore_source_custody_invalid") < restore.index("RESTORE_SECRET_OWNED=1")
    assert restore.index("RESTORE_SECRET_OWNED=1") < restore.index("sudo install -d")
    assert restore.index("RESTORE_STARTED=1") < restore.index("up -d db")
    assert restore.index("up -d db") < restore.index("RESTORE_DB_HEALTH_ATTEMPT")
    assert restore.index("restore_database_health_timeout") < restore.index(
        'RESTORE_RECEIPT="$(sudo scripts/ops/restore.sh'
    )
    assert restore.index("RESTORE_STATUS=$?") < restore.index(
        '[ "$RESTORE_STATUS" -eq 0 ] || exit "$RESTORE_STATUS"'
    )
    assert restore.index('[ "$RESTORE_STATUS" -eq 0 ]') < restore.index("up -d migrate app")
    assert restore.index("up -d migrate app") < restore.index(runtime_probe)
    assert restore.index(runtime_probe) < restore.rindex("cleanup_restore_attempt")
    assert restore.rindex("cleanup_restore_attempt") < restore.rindex(
        '''printf '%s\\n' "$RESTORE_RECEIPT"'''
    )


def test_backup_copies_then_verifies_the_copied_key_against_database_state() -> None:
    backup = _text("scripts/ops/backup.sh")
    copy_key = "cp /source/vault_master_key /output/.vault_master_key.tmp"
    capture_state = '"$ROOT/scripts/ops/database-state.sh" "$DB_CONTAINER"'
    verify_copied = "--vault-key /key/vault_master_key"
    dump_database = "pg_dump --username nvidia_build_lb"

    assert backup.index(copy_key) < backup.index(capture_state)
    assert backup.index(capture_state) < backup.index(verify_copied)
    assert backup.index(verify_copied) < backup.index(dump_database)
    assert "type=bind,source=$key_directory,target=/key,readonly" in backup


def test_backup_withholds_pass_until_state_cleanup_commits() -> None:
    backup = _text("scripts/ops/backup.sh")
    create_receipt = "BACKUP_RECEIPT=$(docker run"
    cleanup_state = "backup_state_cleanup_failed"
    publish_receipt = '''printf '%s\\n' "$BACKUP_RECEIPT"'''

    assert backup.index(create_receipt) < backup.index(cleanup_state)
    assert backup.index(cleanup_state) < backup.index(publish_receipt)


def test_backup_cleanup_failure_never_publishes_a_pass_receipt(tmp_path: Path) -> None:
    execution = _run_backup_with_cleanup_failure(tmp_path)

    assert execution.completed.returncode != 0
    assert execution.completed.stdout == ""
    assert execution.completed.stderr.endswith("backup_state_cleanup_failed\n")
    assert execution.manifest.is_file()
    assert execution.database_state.is_file()


def test_backup_lock_inode_survives_two_failed_contenders(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_jq = fake_bin / "jq"
    _ = fake_docker.write_text(_FAKE_BACKUP_DOCKER, encoding="utf-8")
    _ = fake_jq.write_text(_FAKE_RESTORE_JQ, encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_jq.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database_root = tmp_path / "database"
    key_root = tmp_path / "key"
    manifest_root = tmp_path / "manifest"
    for root in (database_root, key_root, manifest_root):
        root.mkdir(mode=0o700)
    vault_key = tmp_path / "vault_master_key"
    _ = vault_key.write_bytes(_RESTORE_KEY)
    vault_key.chmod(0o600)
    command = [
        str(_ROOT / "scripts/ops/backup.sh"),
        "--db-container",
        "b" * 64,
        "--app-container",
        "c" * 64,
        "--helper-image",
        f"sha256:{'a' * 64}",
        "--vault-key-file",
        str(vault_key),
        "--database-root",
        str(database_root),
        "--key-root",
        str(key_root),
        "--manifest-root",
        str(manifest_root),
        "--backup-id",
        "backup-contention",
    ]
    signal = tmp_path / "holder-ready"
    release = tmp_path / "holder-release"
    base_environment = os.environ.copy()
    base_environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{base_environment['PATH']}",
            "NBLB_OPERATION_LOCK_DIR": str(runtime),
        }
    )
    holder_environment = base_environment | {
        "NBLB_FAKE_HOLD_SIGNAL": str(signal),
        "NBLB_FAKE_HOLD_RELEASE": str(release),
    }
    holder = subprocess.Popen(  # noqa: S603 - fixed repository backup entrypoint.
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_environment,
    )
    holder_stdout = ""
    holder_stderr = ""
    try:
        _wait_for_path(signal)
        lock_files = tuple(runtime.glob("backup-*.lock"))
        assert len(lock_files) == 1
        lock_inode = lock_files[0].stat().st_ino
        contender_environment = base_environment | {"NBLB_FAKE_FAIL_AT": "after-lock"}
        contenders = tuple(
            subprocess.run(  # noqa: S603 - fixed repository backup entrypoint.
                command,
                check=False,
                capture_output=True,
                text=True,
                env=contender_environment,
            )
            for _ in range(2)
        )
        assert all(result.returncode != 0 for result in contenders)
        assert all(result.stdout == "" for result in contenders)
        assert all(result.stderr.endswith("backup_lock_busy\n") for result in contenders)
        assert lock_files[0].stat().st_ino == lock_inode
    finally:
        release.touch()
        holder_stdout, holder_stderr = holder.communicate(timeout=15)
    assert holder.returncode == 0, holder_stderr
    assert holder_stdout == '{"schema_version":2,"status":"PASS"}\n'
    assert tuple(runtime.glob("backup-*.lock"))


def test_restore_snapshots_the_pair_then_rechecks_the_installed_key() -> None:
    restore = _text("scripts/ops/restore.sh")
    snapshot = "RESTORE_STAGE_DIR=$(mktemp -d /tmp/nblb-restore-stage.XXXXXX)"
    verify_snapshot = "--database-dump /stage/database.dump"
    install_key = "cp /stage/vault_master_key /target/.vault_master_key.tmp"
    install_started = "KEY_INSTALL_ATTEMPTED=1"
    verify_installed_artifact = "installed_key_artifact_mismatch"
    restore_database = '"$HELPER_IMAGE" /stage/database.dump'
    verify_installed_database = "installed_key_database_mismatch"
    compare_state = "--manifest /stage/manifest.json"

    assert restore.index(snapshot) < restore.index(verify_snapshot)
    assert restore.index(verify_snapshot) < restore.index(install_started)
    assert restore.index(install_started) < restore.index(install_key)
    assert restore.index(install_key) < restore.index(verify_installed_artifact)
    assert restore.index(verify_installed_artifact) < restore.index(restore_database)
    assert restore.index(restore_database) < restore.index(verify_installed_database)
    assert restore.index(verify_installed_database) < restore.rindex(compare_state)
    assert 'if [ "$status" -ne 0 ] && [ "$KEY_INSTALL_ATTEMPTED" -eq 1 ]' in restore
    assert "rm -f /target/.vault_master_key.tmp /target/vault_master_key" in restore
    assert "marker=/target/.nblb-restore-install" in restore
    assert 'test "$(cat "$marker")" = "$1"' in restore
    assert "restore_key_commit_failed" in restore
    assert "RESTORE_RECEIPT=$(docker run" in restore
    state_cleanup = "remove_state_directory || fail restore_state_cleanup_failed"
    stage_cleanup = "remove_stage_directory || fail restore_stage_cleanup_failed"
    publish = '''printf '%s\\n' "$RESTORE_RECEIPT"'''
    assert restore.rindex(compare_state) < restore.rindex(state_cleanup)
    assert restore.rindex(state_cleanup) < restore.rindex(stage_cleanup)
    assert restore.rindex(stage_cleanup) < restore.rindex("restore_key_commit_failed")
    assert restore.rindex("restore_key_commit_failed") < restore.rindex(publish)
    commit_start = restore.rindex("marker=/target/.nblb-restore-install")
    commit_end = restore.index("\n    helper", commit_start)
    commit = restore[commit_start:commit_end]
    marker_unlink = 'rm -f "$marker"'
    assert commit.index(marker_unlink) < commit.index(
        "sync -f /target", commit.index(marker_unlink)
    )
    after_snapshot = restore[restore.index(verify_snapshot) :]
    assert "source=$DATABASE_ROOT,target=/database-root" not in after_snapshot
    assert "source=$KEY_ROOT,target=/key-root" not in after_snapshot
    assert "source=$MANIFEST_ROOT,target=/manifest-root" not in after_snapshot


def test_restore_uses_the_snapshot_after_a_deterministic_source_swap(tmp_path: Path) -> None:
    execution = _run_restore_with_source_swap(tmp_path)

    assert execution.completed.returncode == 0, execution.completed.stderr
    assert execution.completed.stdout == '{"schema_version":2,"status":"PASS"}\n'
    assert execution.target_key.read_bytes() == _RESTORE_KEY
    assert execution.database_source.read_bytes() == _SWAPPED_DATABASE
    assert execution.key_source.read_bytes() == _SWAPPED_KEY
    assert execution.manifest_source.read_bytes() == _SWAPPED_MANIFEST
    assert not (execution.target_key.parent / ".nblb-restore-install").exists()
    assert not execution.stage_path.exists()


def test_restore_failure_removes_the_installed_key_and_snapshot(tmp_path: Path) -> None:
    execution = _run_restore_with_source_swap(tmp_path, fail_at="install-after-mv")

    assert execution.completed.returncode != 0
    assert execution.completed.stdout == ""
    assert execution.completed.stderr.endswith("restore_key_install_failed\n")
    assert not execution.target_key.exists()
    assert execution.database_source.read_bytes() == _SWAPPED_DATABASE
    assert execution.key_source.read_bytes() == _SWAPPED_KEY
    assert execution.manifest_source.read_bytes() == _SWAPPED_MANIFEST
    assert not execution.stage_path.exists()


def test_restore_preserves_a_key_that_predates_the_install_attempt(tmp_path: Path) -> None:
    preexisting_key = b"p" * 32
    execution = _run_restore_with_source_swap(tmp_path, preexisting_key=preexisting_key)

    assert execution.completed.returncode != 0
    assert execution.completed.stdout == ""
    assert execution.completed.stderr.endswith("restore_key_install_failed\n")
    assert execution.target_key.read_bytes() == preexisting_key
    assert not (execution.target_key.parent / ".nblb-restore-install").exists()
    assert not execution.stage_path.exists()


def test_restore_does_not_emit_pass_before_marker_commit(tmp_path: Path) -> None:
    execution = _run_restore_with_source_swap(tmp_path, fail_at="marker-commit")

    assert execution.completed.returncode != 0
    assert execution.completed.stdout == ""
    assert execution.completed.stderr.endswith("restore_key_commit_failed\n")
    assert not execution.target_key.exists()
    assert not (execution.target_key.parent / ".nblb-restore-install").exists()
    assert not execution.stage_path.exists()


def test_restore_marker_commit_sync_failure_revokes_attempt_owned_key(
    tmp_path: Path,
) -> None:
    execution = _run_restore_with_source_swap(
        tmp_path,
        fail_at="marker-commit-after-unlink",
    )

    assert execution.completed.returncode != 0
    assert execution.completed.stdout == ""
    assert execution.completed.stderr.endswith("restore_key_commit_failed\n")
    assert not execution.target_key.exists()
    assert not (execution.target_key.parent / ".nblb-restore-install").exists()
    assert not execution.stage_path.exists()


def test_restore_cleanup_failure_revokes_key_and_withholds_pass(tmp_path: Path) -> None:
    execution = _run_restore_with_source_swap(tmp_path, fail_at="state-cleanup")

    assert execution.completed.returncode != 0
    assert execution.completed.stdout == ""
    assert execution.completed.stderr.endswith("restore_state_cleanup_failed\n")
    assert not execution.target_key.exists()
    assert not (execution.target_key.parent / ".nblb-restore-install").exists()
    assert not execution.stage_path.exists()


def test_restore_lock_inode_survives_two_failed_contenders(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_jq = fake_bin / "jq"
    _ = fake_docker.write_text(_FAKE_RESTORE_DOCKER, encoding="utf-8")
    _ = fake_jq.write_text(_FAKE_RESTORE_JQ, encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_jq.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    backup_id = "restore-contention"
    database_source = tmp_path / "database" / backup_id / "database.dump"
    key_source = tmp_path / "key" / backup_id / "vault_master_key"
    manifest_source = tmp_path / "manifest" / backup_id / "manifest.json"
    for source in (database_source, key_source, manifest_source):
        source.parent.mkdir(parents=True)
    _ = database_source.write_bytes(_RESTORE_DATABASE)
    _ = key_source.write_bytes(_RESTORE_KEY)
    _ = manifest_source.write_bytes(_RESTORE_MANIFEST)
    for source in (database_source, key_source, manifest_source):
        source.chmod(0o600)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    stage_path_file = tmp_path / "stage-path"
    command = [
        str(_ROOT / "scripts/ops/restore.sh"),
        "--db-container",
        "b" * 64,
        "--helper-image",
        f"sha256:{'a' * 64}",
        "--database-directory",
        str(database_source.parent),
        "--key-directory",
        str(key_source.parent),
        "--manifest",
        str(manifest_source),
        "--target-secret-dir",
        str(target),
    ]
    signal = tmp_path / "holder-ready"
    release = tmp_path / "holder-release"
    base_environment = os.environ.copy()
    base_environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{base_environment['PATH']}",
            "NBLB_FAKE_BACKUP_ID": backup_id,
            "NBLB_FAKE_DATABASE_SHA256": sha256(_RESTORE_DATABASE).hexdigest(),
            "NBLB_FAKE_KEY_SHA256": sha256(_RESTORE_KEY).hexdigest(),
            "NBLB_FAKE_MANIFEST_SHA256": sha256(_RESTORE_MANIFEST).hexdigest(),
            "NBLB_FAKE_STAGE_PATH_FILE": str(stage_path_file),
            "NBLB_OPERATION_LOCK_DIR": str(runtime),
        }
    )
    holder_environment = base_environment | {
        "NBLB_FAKE_HOLD_SIGNAL": str(signal),
        "NBLB_FAKE_HOLD_RELEASE": str(release),
    }
    holder = subprocess.Popen(  # noqa: S603 - fixed repository restore entrypoint.
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=holder_environment,
    )
    holder_stdout = ""
    holder_stderr = ""
    try:
        _wait_for_path(signal)
        lock_files = tuple(runtime.glob("restore-*.lock"))
        assert len(lock_files) == 1
        lock_inode = lock_files[0].stat().st_ino
        contender_environment = base_environment | {"NBLB_FAKE_FAIL_AT": "after-lock"}
        contenders = tuple(
            subprocess.run(  # noqa: S603 - fixed repository restore entrypoint.
                command,
                check=False,
                capture_output=True,
                text=True,
                env=contender_environment,
            )
            for _ in range(2)
        )
        assert all(result.returncode != 0 for result in contenders)
        assert all(result.stdout == "" for result in contenders)
        assert all(result.stderr.endswith("restore_lock_busy\n") for result in contenders)
        assert lock_files[0].stat().st_ino == lock_inode
    finally:
        release.touch()
        holder_stdout, holder_stderr = holder.communicate(timeout=15)
    assert holder.returncode == 0, holder_stderr
    assert holder_stdout == '{"schema_version":2,"status":"PASS"}\n'
    assert tuple(runtime.glob("restore-*.lock"))


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
    assert verify.count('NBLB_OPERATION_LOCK_DIR="$OPERATION_LOCK_DIR"') == 2
    assert "POSTGRES_IMAGE_DIGEST" in verify
    assert "SOURCE_MANIFEST" in verify
    assert "uv run python -m scripts.qa.source_manifest" in verify
    assert "uv run python scripts/qa/source_manifest.py" not in verify
    assert verify.count('cp "$SOURCE_MANIFEST" "$EVIDENCE_DIR/source-manifest.json"') == 1
    assert 'cmp "$CLIENT_DIR/source-manifest.json" "$EVIDENCE_DIR/source-manifest.json"' in verify
    assert "stopped-db-health.json" in verify
    assert '"$stopped_db_status" = 503' in verify
    assert "primary_compose stop db >/dev/null &" in verify
    assert "db_stop_pid=$!" in verify
    assert "--connect-timeout 1 --max-time 1" in verify
    assert 'wait "$db_stop_pid"' in verify
    assert verify.index("primary_compose stop db >/dev/null &") < verify.index(
        'wait "$db_stop_pid"'
    )
    assert "alembic_version" in verify
    assert "migration_failed" in verify
    assert "pip-audit" in scan
    assert "gitleaks" in scan
    assert "trivy" in scan
    assert "POSTGRES_IMAGE_DIGEST" in scan
    assert "SOURCE_MANIFEST" in scan
    assert scan.count("uv run python -m scripts.qa.source_manifest") == 2
    assert "uv run python scripts/qa/source_manifest.py" not in scan
    assert "--ignore-unfixed" not in scan
    assert "trivy-postgres-image.json" in scan
    assert "FILESYSTEM_SECRETS" in scan
    assert "filesystem_secrets" in scan
    assert scan.count('--user "$(id -u):$(id -g)"') >= 5
    assert "gitleaks_source_incomplete" in scan
    assert "docker history --no-trunc" in scan
    assert "mapfile -t python_base_images" in scan
    assert "candidate_public_gpg_key_drift" in scan
    assert "candidate_public_gpg_history_drift" in scan
    assert scan.count("verified-pinned-base-public-key") == 2
    assert "pinned_base_public_gpg_key_verified" in scan
    assert "uses:" in scan


def test_local_verifier_observes_database_grace_and_old_app_exit() -> None:
    verify = _text("scripts/qa/verify-local.sh")

    assert "database_failure_app_id=$(primary_compose ps -q app)" in verify
    assert 'old_app_exit_state=$(wait_container_exit "$database_failure_app_id")' in verify
    assert "degraded_observed_epoch_ns=$(date -u +%s%N)" in verify
    assert "old_app_finished_at=$(docker inspect --format '{{.State.FinishedAt}}'" in verify
    assert 'old_app_exit_epoch_ns=$(date -u --date="$old_app_finished_at" +%s%N)' in verify
    assert "old_app_grace_ms=$((old_app_grace_ns / 1000000))" in verify
    assert '"$old_app_grace_ms" -ge 1500' in verify
    assert '"$old_app_grace_ms" -le 15000' in verify
    assert "old_app_same_container_exited:true" in verify
    assert "replacement_container_new:true" in verify
    assert verify.index('wait_container_exit "$database_failure_app_id"') < verify.index(
        "primary_compose start db"
    )
    assert "chown 0:0 /secrets /secrets/*" in verify
    assert 'chown "$1:$2" /target; chmod 0700 /target' in verify


def test_release_scanner_rejects_shallow_history_before_gitleaks() -> None:
    scan = _text("scripts/qa/scan-release.sh")

    assert "git rev-parse --is-shallow-repository" in scan
    assert "git_history_shallow" in scan
    assert scan.index("git rev-parse --is-shallow-repository") < scan.index(
        '"$GITLEAKS_IMAGE" git /repo'
    )


def test_production_compose_keeps_the_release_boundary_closed() -> None:
    compose = _text("compose.yml")

    app_reference = (
        "ghcr.io/dongwonttuna-labs/nvidia-build-lb@sha256:"
        "${NBLB_APP_REGISTRY_DIGEST:?raw application registry digest required}"
    )
    postgres_reference = (
        "ghcr.io/dongwonttuna-labs/nvidia-build-lb@sha256:"
        "${NBLB_POSTGRES_REGISTRY_DIGEST:?raw PostgreSQL registry digest required}"
    )
    assert compose.count(app_reference) == 2
    assert compose.count(postgres_reference) == 1
    assert "NBLB_APP_IMAGE" not in compose
    assert "NBLB_POSTGRES_IMAGE" not in compose
    assert "127.0.0.1:${NBLB_PORT:-2456}:2456" in compose
    assert "NBLB_BIND_ADDRESS" not in compose
    assert "nvidia-build-lb.restore-isolated: ${NBLB_RESTORE_ISOLATED:-false}" in compose
    assert "nvidia-build-lb.backup-source: ${NBLB_BACKUP_SOURCE:-true}" in compose
    assert compose.count('com.centurylinklabs.watchtower.enable: "false"') == 3
    assert "internal: true" in compose
    assert "latest" not in compose.lower()


def test_operations_docs_keep_the_release_boundary_closed() -> None:
    backup = _text("docs/BACKUP_RESTORE.md")
    design = _text("DESIGN.md")
    security = _text("docs/SECURITY.md")
    rollback = _text("docs/ROLLBACK.md")
    runbook = _text("docs/RUNBOOK.md")
    assert "restore-isolated=true" in backup
    assert "Never restore over the live volume" in backup
    assert "Do not rotate `vault_master_key` in isolation" in runbook
    assert "full 40-hex commit pins" in security
    assert ".omo/evidence/task-6b-release" not in security
    assert 'FINAL_BASE=".omo/evidence/final-$RUN_ID"' in security
    assert "BROWSER_FRESH_STATUS" in security
    assert "REVIEW_REQUIRED" in security
    assert "scripts/qa/record_visual_review.py" in security
    assert "visual_reviews.pass_a" in security
    assert "Any blocker or source/image/manifest/artifact drift" in security
    assert "Do not run Alembic downgrade" in rollback
    recovery = runbook[runbook.index("## Ledger-capacity forward recovery") :]
    assert "/etc/nvidia-build-lb/runtime.env.lock" in runbook
    assert "sudo test ! -e /etc/nvidia-build-lb/runtime.env.lock" in runbook
    assert "recover-ledger-capacity 200000 80000 2000" in recovery
    assert "update-ledger-caps 200000 80000 2000" not in recovery
    assert "stops `app` and leaves intake withdrawn" in recovery
    assert "RESTORE_RUNTIME_ATTEMPT" in backup
    assert "restore_runtime_timeout" in backup
    assert "ROLLBACK_RUNTIME_ATTEMPT" in rollback
    assert "rollback_runtime_timeout" in rollback
    assert "No intermediate cap value" in design
    assert "Permanent evidence blockers never enter this path" in design
    assert "independent of eligible-key readiness" in design
    assert "empty first-run" in design
    assert "target-image-independent host checker" in design
    assert "legacy-overview fallback" in design
    assert "runtime.env.lock" in rollback
    assert "exclude competing rollout operators" in rollback
    trivy_exceptions = [
        line for line in _text(".trivyignore").splitlines() if line and not line.startswith("#")
    ]
    assert trivy_exceptions == ["DS-0002"]


def test_operator_probe_docs_name_evidence_and_withdraw_failed_rollback() -> None:
    backup = _text("docs/BACKUP_RESTORE.md")
    design = _text("DESIGN.md")
    rollback = _text("docs/ROLLBACK.md")
    runbook = _text("docs/RUNBOOK.md")

    assert backup.count("authenticated runtime probe") == 2
    assert "Only that route's `404`" in backup
    assert "same container ID and `StartedAt` generation" in backup
    assert "operational evidence is unconfirmed" in backup
    assert "canonical service `Host`" in rollback
    assert "ledger-capacity` mode used by forward recovery has no legacy" in rollback
    assert "two-second absolute deadline" in design
    assert "second exact sample 30 seconds" in design
    assert "This `ledger-capacity` mode" in runbook
    assert "never uses the legacy-overview fallback" in runbook
    assert "ROLLBACK_APP_MUST_WITHDRAW=1" in rollback
    assert "rollback_candidate_withdraw_failed" in rollback
    assert "trap rollback_exit EXIT" in rollback
    assert "trap 'exit 129' HUP" in rollback
    assert "trap 'exit 130' INT" in rollback
    assert "trap 'exit 143' TERM" in rollback
    assert rollback.index("ROLLBACK_APP_MUST_WITHDRAW=1") < rollback.index(
        "up -d --force-recreate --no-deps app"
    )
    assert rollback.index("curl --fail http://127.0.0.1:2455/health") < rollback.rindex(
        "ROLLBACK_APP_MUST_WITHDRAW=0"
    )


def test_rollback_document_trap_withdraws_failed_candidate(tmp_path: Path) -> None:
    rollback = _text("docs/ROLLBACK.md")
    start = rollback.index("ROLLBACK_APP_MUST_WITHDRAW=0")
    end_marker = "trap 'exit 143' TERM"
    trap_end = rollback.index(end_marker, start) + len(end_marker)
    trap_setup = rollback[start:trap_end]
    fake_wrapper = tmp_path / "scripts/ops/production-compose.sh"
    fake_wrapper.parent.mkdir(parents=True)
    _ = fake_wrapper.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$NBLB_ROLLBACK_LOG"\n',
        encoding="utf-8",
    )
    fake_wrapper.chmod(0o755)
    command_log = tmp_path / "rollback.log"
    environment = os.environ.copy()
    environment["NBLB_ROLLBACK_LOG"] = str(command_log)

    completed = subprocess.run(  # noqa: S603 - exact documented trap under test.
        [
            "/usr/bin/bash",
            "-c",
            f"{trap_setup}\nROLLBACK_APP_MUST_WITHDRAW=1\nfalse\n",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert command_log.read_text(encoding="utf-8") == "stop app\n"


@pytest.mark.parametrize(
    ("interruption", "expected_status"),
    [
        (signal.SIGHUP, 129),
        (signal.SIGINT, 130),
        (signal.SIGTERM, 143),
    ],
)
@pytest.mark.parametrize("foreground_child", [False, True], ids=("boundary", "child"))
def test_rollback_document_signal_withdraws_and_preserves_signal_status(
    tmp_path: Path,
    interruption: signal.Signals,
    expected_status: int,
    foreground_child: bool,
) -> None:
    rollback = _text("docs/ROLLBACK.md")
    start = rollback.index("ROLLBACK_APP_MUST_WITHDRAW=0")
    end_marker = "trap 'exit 143' TERM"
    trap_end = rollback.index(end_marker, start) + len(end_marker)
    trap_setup = rollback[start:trap_end]
    fake_wrapper = tmp_path / "scripts/ops/production-compose.sh"
    fake_wrapper.parent.mkdir(parents=True)
    _ = fake_wrapper.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$NBLB_ROLLBACK_LOG"\n',
        encoding="utf-8",
    )
    fake_wrapper.chmod(0o755)
    command_log = tmp_path / "rollback.log"
    ready = tmp_path / "signal-ready"
    child_ready = tmp_path / "child-ready"
    foreground_wrapper = tmp_path / "scripts/foreground-child.sh"
    _ = foreground_wrapper.write_text(
        '#!/bin/sh\n: > "$NBLB_ROLLBACK_CHILD_READY"\nexec /usr/bin/sleep 30\n',
        encoding="utf-8",
    )
    foreground_wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "NBLB_ROLLBACK_CHILD_READY": str(child_ready),
            "NBLB_ROLLBACK_LOG": str(command_log),
            "NBLB_ROLLBACK_READY": str(ready),
        }
    )
    blocked_command = "scripts/foreground-child.sh" if foreground_child else "while :; do :; done"
    signal_tail = 'ROLLBACK_APP_MUST_WITHDRAW=1\n: > "$NBLB_ROLLBACK_READY"'
    signal_script = f"{trap_setup}\n{signal_tail}\n{blocked_command}"
    rollback_process = subprocess.Popen(  # noqa: S603 - exact documented traps under test.
        [
            "/usr/bin/bash",
            "-c",
            signal_script,
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_for_path(ready)
        if foreground_child:
            _wait_for_path(child_ready)
            os.killpg(rollback_process.pid, interruption)
        else:
            os.kill(rollback_process.pid, interruption)
        stdout, stderr = rollback_process.communicate(timeout=5)
    finally:
        if rollback_process.poll() is None:
            os.killpg(rollback_process.pid, signal.SIGKILL)
            _ = rollback_process.communicate(timeout=5)

    assert rollback_process.returncode == expected_status, stderr
    assert stdout == ""
    if foreground_child:
        assert "rollback_candidate_withdraw_failed" not in stderr
        assert "foreground-child.sh" in stderr or stderr == ""
    else:
        assert stderr == ""
    assert command_log.read_text(encoding="utf-8") == "stop app\n"


def test_production_compose_wrapper_rejects_mutable_or_malformed_images(
    tmp_path: Path,
) -> None:
    wrapper = _ROOT / "scripts/ops/production-compose.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    _ = fake_docker.write_text(
        r"""#!/bin/sh
printf '%s\n' \
  "$NBLB_APP_REGISTRY_DIGEST" \
  "$NBLB_ADMIN_EVENT_MAX_ROWS" \
  "$NBLB_ADMIN_ATTEMPT_MAX_ROWS" \
  "$NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE" \
  "$@"
""",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    runtime_config = tmp_path / "runtime.env"
    runtime_lock = tmp_path / "runtime.env.lock"
    _ = runtime_lock.write_text("", encoding="utf-8")
    runtime_lock.chmod(0o644)

    def write_config(app_digest: str, *, event_rows: int = 100_000) -> None:
        _ = runtime_config.write_text(
            "\n".join(
                (
                    f"NBLB_APP_REGISTRY_DIGEST={app_digest}",
                    f"NBLB_POSTGRES_REGISTRY_DIGEST={'b' * 64}",
                    "NBLB_SECRET_DIR=/opt/nvidia-build-lb/secrets",
                    f"NBLB_ADMIN_EVENT_MAX_ROWS={event_rows}",
                    "NBLB_ADMIN_ATTEMPT_MAX_ROWS=40000",
                    "NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=1000",
                    "",
                )
            ),
            encoding="utf-8",
        )
        runtime_config.chmod(0o644)

    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["NBLB_RUNTIME_CONFIG_FILE"] = str(runtime_config)

    for rejected in ("latest", "sha256:" + "a" * 64, "A" * 64, "a" * 63):
        write_config(rejected)
        completed = subprocess.run(  # noqa: S603 - fixed local wrapper under test.
            [wrapper, "config", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert completed.returncode != 0
        assert completed.stderr == "app_registry_digest_invalid\n"

    write_config("a" * 64)
    environment["NBLB_APP_REGISTRY_DIGEST"] = "c" * 64
    environment["NBLB_ADMIN_EVENT_MAX_ROWS"] = "999999"
    accepted = subprocess.run(  # noqa: S603 - fixed local wrapper under test.
        [wrapper, "config", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert accepted.returncode == 0
    assert accepted.stdout.startswith(f"{'a' * 64}\n100000\n40000\n1000\n")
    assert "compose\n" in accepted.stdout
    assert f"{_ROOT / 'compose.yml'}\n" in accepted.stdout
    assert accepted.stdout.endswith("config\n--quiet\n")

    updated = subprocess.run(  # noqa: S603 - fixed local wrapper under test.
        [wrapper, "update-ledger-caps", "200000", "80000", "2000"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert updated.returncode == 0, updated.stderr
    assert updated.stdout == "runtime_config_updated\n"
    assert runtime_config.read_text(encoding="utf-8").splitlines() == [
        f"NBLB_APP_REGISTRY_DIGEST={'a' * 64}",
        f"NBLB_POSTGRES_REGISTRY_DIGEST={'b' * 64}",
        "NBLB_SECRET_DIR=/opt/nvidia-build-lb/secrets",
        "NBLB_ADMIN_EVENT_MAX_ROWS=200000",
        "NBLB_ADMIN_ATTEMPT_MAX_ROWS=80000",
        "NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=2000",
    ]
    persisted = subprocess.run(  # noqa: S603 - fixed local wrapper under test.
        [wrapper, "config", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert persisted.returncode == 0, persisted.stderr
    assert persisted.stdout.startswith(f"{'a' * 64}\n200000\n80000\n2000\n")

    image_updated = subprocess.run(  # noqa: S603 - fixed local wrapper under test.
        [wrapper, "update-app-digest", "d" * 64],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert image_updated.returncode == 0, image_updated.stderr
    updated_lines = runtime_config.read_text(encoding="utf-8").splitlines()
    assert updated_lines[0] == f"NBLB_APP_REGISTRY_DIGEST={'d' * 64}"
    assert updated_lines[-3:] == [
        "NBLB_ADMIN_EVENT_MAX_ROWS=200000",
        "NBLB_ADMIN_ATTEMPT_MAX_ROWS=80000",
        "NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=2000",
    ]


@dataclass(frozen=True, slots=True)
class _RuntimeRecoveryHarness:
    wrapper: Path
    environment: dict[str, str]
    runtime_config: Path
    command_log: Path
    capacity_count: Path
    capacity_started: Path
    recreated: Path
    stopped: Path
    up_started: Path
    up_release: Path

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed repository wrapper under test.
            [self.wrapper, *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=self.environment,
        )


_FAKE_RUNTIME_DOCKER = r"""#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

ARGS = sys.argv[1:]
LOG = Path(os.environ["NBLB_FAKE_COMMAND_LOG"])
PHASE = Path(os.environ["NBLB_FAKE_PHASE"])
STOPPED = Path(os.environ["NBLB_FAKE_STOPPED"])
BEFORE = "1" * 64
AFTER = "2" * 64
EXPECTED_ID = "sha256:" + ("3" * 64)
EXPECTED_REF = (
    "ghcr.io/dongwonttuna-labs/nvidia-build-lb@sha256:"
    + os.environ["NBLB_APP_REGISTRY_DIGEST"]
)


def record(value):
    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(value + "\n")


if ARGS[:2] == ["image", "inspect"]:
    print(EXPECTED_ID)
    raise SystemExit(0)

if ARGS and ARGS[0] == "inspect":
    template = ARGS[ARGS.index("--format") + 1]
    container = ARGS[-1]
    after = container == AFTER
    if "Config.Image" in template:
        if not after and os.environ.get("NBLB_FAKE_PRESTART_MISMATCH") == "1":
            print("ghcr.io/example/mismatched@sha256:" + ("4" * 64))
        elif after and os.environ.get("NBLB_FAKE_POSTSTART_MISMATCH") == "1":
            print("ghcr.io/example/mismatched@sha256:" + ("5" * 64))
        else:
            print(EXPECTED_REF)
    elif template == "{{.Image}}":
        print(EXPECTED_ID)
    elif "State.Running" in template:
        print("false" if STOPPED.exists() else "true")
    else:
        raise SystemExit(91)
    raise SystemExit(0)

if not ARGS or ARGS[0] != "compose":
    raise SystemExit(92)

if "ps" in ARGS:
    print(AFTER if PHASE.exists() else BEFORE)
    raise SystemExit(0)

if "config" in ARGS:
    record("config:" + os.environ["NBLB_ADMIN_EVENT_MAX_ROWS"])
    raise SystemExit(0)

if "up" in ARGS:
    record("recreate:" + os.environ["NBLB_ADMIN_EVENT_MAX_ROWS"])
    runtime_path = os.environ.get("NBLB_FAKE_MUTATE_RUNTIME_ON_UP")
    if runtime_path:
        runtime = Path(runtime_path)
        current = runtime.read_text(encoding="utf-8")
        runtime.write_text(
            current.replace(
                "NBLB_ADMIN_EVENT_MAX_ROWS=100000",
                "NBLB_ADMIN_EVENT_MAX_ROWS=300000",
            ),
            encoding="utf-8",
        )
    if os.environ.get("NBLB_FAKE_BLOCK_UP") == "1":
        Path(os.environ["NBLB_FAKE_UP_STARTED"]).touch()
        release = Path(os.environ["NBLB_FAKE_UP_RELEASE"])
        deadline = time.monotonic() + 10
        while not release.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not release.exists():
            raise SystemExit(93)
    if os.environ.get("NBLB_FAKE_RECREATE_FAIL") == "1":
        raise SystemExit(94)
    STOPPED.unlink(missing_ok=True)
    PHASE.touch()
    raise SystemExit(0)

if "stop" in ARGS:
    record("stop")
    STOPPED.touch()
    raise SystemExit(0)

raise SystemExit(95)
"""

_FAKE_RUNTIME_PYTHON = r"""#!__PYTHON__
import os
import sys
import time
from pathlib import Path

ARGS = sys.argv[1:]
REAL_PYTHON = "__PYTHON__"
if not ARGS or Path(ARGS[0]).name != "operator_readiness_probe.py":
    os.execv(REAL_PYTHON, [REAL_PYTHON, *ARGS])
if len(ARGS) != 4 or ARGS[1:] != [
    "ledger-capacity",
    "2456",
    "/opt/nvidia-build-lb/secrets/admin_token",
]:
    raise SystemExit(96)
counter = Path(os.environ["NBLB_FAKE_CAPACITY_COUNT"])
count = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
counter.write_text(str(count), encoding="utf-8")
if os.environ.get("NBLB_FAKE_BLOCK_CAPACITY") == "1":
    Path(os.environ["NBLB_FAKE_CAPACITY_STARTED"]).touch()
    release = Path(os.environ["NBLB_FAKE_CAPACITY_RELEASE"])
    deadline = time.monotonic() + 10
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not release.exists():
        raise SystemExit(97)
if os.environ.get("NBLB_FAKE_CAPACITY_ALWAYS_FAIL") == "1":
    raise SystemExit(22)
required = int(os.environ.get("NBLB_FAKE_CAPACITY_SUCCEED_AFTER", "1"))
raise SystemExit(0 if count >= required else 22)
"""

_FAKE_RUNTIME_SYNC = r"""#!/usr/bin/env python3
import os
import sys
from pathlib import Path

target = Path(sys.argv[-1])
if (
    os.environ.get("NBLB_FAKE_SYNC_FAIL_TEMP") == "1"
    and target.name.startswith(".nblb-runtime.")
):
    raise SystemExit(1)
raise SystemExit(0)
"""


def _runtime_config_text(*, event_rows: int = 100_000) -> str:
    return "\n".join(
        (
            f"NBLB_APP_REGISTRY_DIGEST={'a' * 64}",
            f"NBLB_POSTGRES_REGISTRY_DIGEST={'b' * 64}",
            "NBLB_SECRET_DIR=/opt/nvidia-build-lb/secrets",
            f"NBLB_ADMIN_EVENT_MAX_ROWS={event_rows}",
            "NBLB_ADMIN_ATTEMPT_MAX_ROWS=40000",
            "NBLB_ADMIN_LEDGER_PRUNE_BATCH_SIZE=1000",
            "",
        )
    )


def _runtime_recovery_harness(tmp_path: Path) -> _RuntimeRecoveryHarness:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    _ = fake_docker.write_text(_FAKE_RUNTIME_DOCKER, encoding="utf-8")
    fake_docker.chmod(0o755)
    fake_python = fake_bin / "python3"
    _ = fake_python.write_text(
        _FAKE_RUNTIME_PYTHON.replace("__PYTHON__", sys.executable),
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_sleep = fake_bin / "sleep"
    _ = fake_sleep.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_sleep.chmod(0o755)
    fake_sync = fake_bin / "sync"
    _ = fake_sync.write_text(_FAKE_RUNTIME_SYNC, encoding="utf-8")
    fake_sync.chmod(0o755)

    runtime_config = tmp_path / "runtime.env"
    _ = runtime_config.write_text(_runtime_config_text(), encoding="utf-8")
    runtime_config.chmod(0o644)
    runtime_lock = tmp_path / "runtime.env.lock"
    _ = runtime_lock.write_text("", encoding="utf-8")
    runtime_lock.chmod(0o644)

    command_log = tmp_path / "commands.log"
    capacity_count = tmp_path / "capacity-count"
    capacity_started = tmp_path / "capacity-started"
    recreated = tmp_path / "after-recreate"
    stopped = tmp_path / "app-stopped"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "NBLB_RUNTIME_CONFIG_FILE": str(runtime_config),
            "NBLB_RUNTIME_LOCK_FILE": str(runtime_lock),
            "NBLB_FAKE_COMMAND_LOG": str(command_log),
            "NBLB_FAKE_PHASE": str(recreated),
            "NBLB_FAKE_STOPPED": str(stopped),
            "NBLB_FAKE_CAPACITY_COUNT": str(capacity_count),
            "NBLB_FAKE_CAPACITY_STARTED": str(capacity_started),
            "NBLB_FAKE_CAPACITY_RELEASE": str(tmp_path / "capacity-release"),
            "NBLB_FAKE_UP_STARTED": str(tmp_path / "up-started"),
            "NBLB_FAKE_UP_RELEASE": str(tmp_path / "up-release"),
        }
    )
    return _RuntimeRecoveryHarness(
        wrapper=_ROOT / "scripts/ops/production-compose.sh",
        environment=environment,
        runtime_config=runtime_config,
        command_log=command_log,
        capacity_count=capacity_count,
        capacity_started=capacity_started,
        recreated=recreated,
        stopped=stopped,
        up_started=tmp_path / "up-started",
        up_release=tmp_path / "up-release",
    )


def test_runtime_recovery_retries_capacity_before_committing_caps(tmp_path: Path) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_CAPACITY_SUCCEED_AFTER"] = "3"

    completed = harness.run("recover-ledger-capacity", "200000", "80000", "2000")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "ledger_capacity_recreated_same_image\n"
    assert harness.capacity_count.read_text(encoding="utf-8") == "3"
    assert "NBLB_ADMIN_EVENT_MAX_ROWS=200000" in harness.runtime_config.read_text(encoding="utf-8")
    assert harness.command_log.read_text(encoding="utf-8").splitlines() == [
        "config:200000",
        "recreate:200000",
    ]


def test_runtime_recovery_prestart_mismatch_never_recreates(tmp_path: Path) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_PRESTART_MISMATCH"] = "1"

    completed = harness.run("recover-ledger-capacity", "200000", "80000", "2000")

    assert completed.returncode != 0
    assert completed.stderr == "runtime_recovery_prestart_image_mismatch\n"
    assert not harness.command_log.exists()
    assert harness.runtime_config.read_text(encoding="utf-8") == _runtime_config_text()


def test_runtime_recovery_poststart_mismatch_withdraws_without_commit(
    tmp_path: Path,
) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_POSTSTART_MISMATCH"] = "1"

    completed = harness.run("recover-ledger-capacity", "200000", "80000", "2000")

    assert completed.returncode != 0
    assert completed.stderr == "runtime_recovery_same_image_mismatch\n"
    assert harness.command_log.read_text(encoding="utf-8").splitlines()[-1] == "stop"
    assert harness.runtime_config.read_text(encoding="utf-8") == _runtime_config_text()


def test_runtime_recovery_capacity_timeout_withdraws_without_commit(tmp_path: Path) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_CAPACITY_ALWAYS_FAIL"] = "1"

    completed = harness.run("recover-ledger-capacity", "200000", "80000", "2000")

    assert completed.returncode != 0
    assert completed.stderr == "runtime_recovery_capacity_failed\n"
    assert harness.capacity_count.read_text(encoding="utf-8") == "30"
    assert harness.command_log.read_text(encoding="utf-8").splitlines()[-1] == "stop"
    assert harness.runtime_config.read_text(encoding="utf-8") == _runtime_config_text()


def test_runtime_recovery_cas_drift_withdraws_and_preserves_external_generation(
    tmp_path: Path,
) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_MUTATE_RUNTIME_ON_UP"] = str(harness.runtime_config)

    completed = harness.run("recover-ledger-capacity", "200000", "80000", "2000")

    assert completed.returncode != 0
    assert completed.stderr == "runtime_recovery_config_changed\n"
    assert harness.command_log.read_text(encoding="utf-8").splitlines()[-1] == "stop"
    current = harness.runtime_config.read_text(encoding="utf-8")
    assert "NBLB_ADMIN_EVENT_MAX_ROWS=300000" in current
    assert "NBLB_ADMIN_EVENT_MAX_ROWS=200000" not in current


def test_runtime_recovery_commit_failure_cleans_scoped_temporary(
    tmp_path: Path,
) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_SYNC_FAIL_TEMP"] = "1"

    completed = harness.run("recover-ledger-capacity", "200000", "80000", "2000")

    assert completed.returncode != 0
    assert completed.stderr == "runtime_recovery_config_commit_failed\n"
    assert harness.command_log.read_text(encoding="utf-8").splitlines()[-1] == "stop"
    assert harness.runtime_config.read_text(encoding="utf-8") == _runtime_config_text()
    assert tuple(tmp_path.glob(".nblb-runtime.*")) == ()


def test_runtime_recovery_reenters_its_same_image_withdrawn_state(tmp_path: Path) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_CAPACITY_ALWAYS_FAIL"] = "1"
    failed = harness.run("recover-ledger-capacity", "200000", "80000", "2000")
    assert failed.stderr == "runtime_recovery_capacity_failed\n"
    assert harness.stopped.exists()

    del harness.environment["NBLB_FAKE_CAPACITY_ALWAYS_FAIL"]
    harness.capacity_count.unlink()
    recovered = harness.run("recover-ledger-capacity", "200000", "80000", "2000")

    assert recovered.returncode == 0, recovered.stderr
    assert not harness.stopped.exists()
    assert "NBLB_ADMIN_EVENT_MAX_ROWS=200000" in harness.runtime_config.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("interruption", "expected_status"),
    [(signal.SIGHUP, 129), (signal.SIGTERM, 143)],
)
def test_runtime_recovery_signal_after_recreate_start_withdraws_before_exit(
    tmp_path: Path,
    interruption: signal.Signals,
    expected_status: int,
) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_BLOCK_CAPACITY"] = "1"
    recovery = subprocess.Popen(  # noqa: S603 - fixed repository wrapper under test.
        [harness.wrapper, "recover-ledger-capacity", "200000", "80000", "2000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=harness.environment,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not harness.capacity_started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert harness.capacity_started.exists()
        assert harness.recreated.exists()
        os.killpg(recovery.pid, interruption)
        _, stderr = recovery.communicate(timeout=5)
    finally:
        if recovery.poll() is None:
            os.killpg(recovery.pid, signal.SIGKILL)
            _ = recovery.communicate(timeout=5)

    assert recovery.returncode == expected_status, stderr
    assert harness.command_log.read_text(encoding="utf-8").splitlines()[-1] == "stop"
    assert harness.stopped.exists()
    assert harness.runtime_config.read_text(encoding="utf-8") == _runtime_config_text()


def test_runtime_recovery_lock_rejects_concurrent_update_without_reverting_caps(
    tmp_path: Path,
) -> None:
    harness = _runtime_recovery_harness(tmp_path)
    harness.environment["NBLB_FAKE_BLOCK_UP"] = "1"
    recovery = subprocess.Popen(  # noqa: S603 - fixed repository wrapper under test.
        [harness.wrapper, "recover-ledger-capacity", "200000", "80000", "2000"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=harness.environment,
    )
    deadline = time.monotonic() + 5
    while not harness.up_started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert harness.up_started.exists(), recovery.communicate(timeout=1)
        concurrent = harness.run("update-ledger-caps", "300000", "100000", "3000")
        assert concurrent.returncode != 0
        assert concurrent.stderr == "runtime_config_lock_busy\n"
    finally:
        harness.up_release.touch()
    stdout, stderr = recovery.communicate(timeout=5)

    assert recovery.returncode == 0, stderr
    assert stdout == "ledger_capacity_recreated_same_image\n"
    current = harness.runtime_config.read_text(encoding="utf-8")
    assert "NBLB_ADMIN_EVENT_MAX_ROWS=200000" in current
    assert "NBLB_ADMIN_EVENT_MAX_ROWS=300000" not in current


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
    assert "workflow_dispatch:" in publish
    assert "push:" not in publish
    assert "docker/build-push-action" not in publish
    assert "scripts/qa/build-candidate.sh" in publish
    assert "scripts/qa/scan-release.sh" in publish
    assert "fetch-depth: 0" in publish
    assert "npm ci --ignore-scripts --no-audit --no-fund" in publish
    assert "uv run playwright install --with-deps chromium" in publish
    assert 'docker tag "$APP_DIGEST"' in publish
    assert 'docker tag "$POSTGRES_DIGEST"' in publish


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
