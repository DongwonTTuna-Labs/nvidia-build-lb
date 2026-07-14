"""Static release, migration, workflow, and shell safety contracts."""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

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

if 'mkdir "/output/$1"' in shell_command:
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

if "rm -f /target/.vault_master_key.tmp /target/vault_master_key" in shell_command:
    marker = bound["/target"] / ".nblb-restore-install"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == ARGS[-1]:
        (bound["/target"] / ".vault_master_key.tmp").unlink(missing_ok=True)
        (bound["/target"] / "vault_master_key").unlink(missing_ok=True)
        marker.unlink()
    raise SystemExit(0)

if "rm -f /state/database-state.json" in shell_command:
    (bound["/state"] / "database-state.json").unlink(missing_ok=True)
    raise SystemExit(0)

if "rm -f /stage/database.dump" in shell_command:
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
if "-cS" in arguments:
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

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "NBLB_FAKE_FAIL_AT": "state-cleanup",
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
    assert restore.rindex("restore_key_commit_failed") < restore.rindex(
        '''printf '%s\\n' "$RESTORE_RECEIPT"'''
    )
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


def test_production_compose_and_operations_docs_keep_the_release_boundary_closed() -> None:
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


def test_production_compose_wrapper_rejects_mutable_or_malformed_images(
    tmp_path: Path,
) -> None:
    wrapper = _ROOT / "scripts/ops/production-compose.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    _ = fake_docker.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["NBLB_POSTGRES_REGISTRY_DIGEST"] = "b" * 64

    for rejected in ("latest", "sha256:" + "a" * 64, "A" * 64, "a" * 63):
        environment["NBLB_APP_REGISTRY_DIGEST"] = rejected
        completed = subprocess.run(  # noqa: S603 - fixed local wrapper under test.
            [wrapper, "config", "--quiet"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert completed.returncode != 0
        assert completed.stderr == "app_registry_digest_invalid\n"

    environment["NBLB_APP_REGISTRY_DIGEST"] = "a" * 64
    accepted = subprocess.run(  # noqa: S603 - fixed local wrapper under test.
        [wrapper, "config", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert accepted.returncode == 0
    assert "compose\n" in accepted.stdout
    assert f"{_ROOT / 'compose.yml'}\n" in accepted.stdout
    assert accepted.stdout.endswith("config\n--quiet\n")


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
