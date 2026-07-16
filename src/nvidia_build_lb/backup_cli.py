"""Value-silent root-only command boundary for paired backup manifests."""

import argparse
import contextlib
import json
import os
import stat
import sys
from pathlib import Path

from nvidia_build_lb.backup_manifest import (
    build_manifest,
    verify_manifest,
    verify_restored_state,
    verify_vault_key_material,
)
from nvidia_build_lb.backup_models import (
    MAX_STATE_BYTES,
    ROOT_DIRECTORY_MODE,
    ROOT_FILE_MODE,
    VAULT_KEY_BYTES,
    BackupContractError,
    BackupManifestValue,
    DatabaseStateV3,
    DatabaseStateValue,
    parse_backup_manifest_json,
    parse_database_state_json,
)


class _Arguments(argparse.Namespace):
    command: str = ""
    backup_id: str = ""
    created_at: str = ""
    database_dump: Path = Path()
    vault_key: Path = Path()
    state: Path = Path()
    manifest: Path = Path()


def _assert_root_regular(path: Path, *, maximum_bytes: int | None = None) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != ROOT_FILE_MODE
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or (maximum_bytes is not None and metadata.st_size > maximum_bytes)
    ):
        reason = "root_artifact_invalid"
        raise BackupContractError(reason)


def _assert_root_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != ROOT_DIRECTORY_MODE
    ):
        reason = "root_directory_invalid"
        raise BackupContractError(reason)


def _load_state(path: Path) -> DatabaseStateValue:
    _assert_root_regular(path, maximum_bytes=MAX_STATE_BYTES)
    return parse_database_state_json(path.read_bytes())


def _load_manifest(path: Path) -> BackupManifestValue:
    _assert_root_regular(path, maximum_bytes=MAX_STATE_BYTES)
    return parse_backup_manifest_json(path.read_bytes())


def _write_manifest(path: Path, manifest: BackupManifestValue) -> None:
    _assert_root_directory(path.parent)
    if path.exists() or path.is_symlink():
        reason = "manifest_exists"
        raise BackupContractError(reason)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = -1
    directory_descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        payload = (manifest.model_dump_json(indent=2) + "\n").encode()
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                reason = "manifest_write_failed"
                raise BackupContractError(reason)
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _ = temporary.replace(path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        os.fsync(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _safe_receipt(manifest: BackupManifestValue, *, restored: bool) -> str:
    payload: dict[str, str | bool | int] = {
        "schema_version": manifest.schema_version,
        "status": "PASS",
        "pair_id": manifest.pair_id,
        "backup_id": manifest.backup_id,
        "restored_state_matches": restored,
        "alembic_revision": manifest.state.alembic_revision,
        "upstream_count": manifest.state.upstream_count,
        "upstream_identity_sha256": manifest.state.upstream_identity_sha256,
        "downstream_count": manifest.state.downstream_count,
        "downstream_digest_sha256": manifest.state.downstream_digest_sha256,
        "vault_key_sha256": manifest.vault_master_key.sha256,
        "vault_key_matches_database": True,
    }
    if isinstance(manifest.state, DatabaseStateV3):
        payload.update(
            {
                "admin_event_count": manifest.state.admin_event_count,
                "admin_event_identity_sha256": manifest.state.admin_event_identity_sha256,
                "attempt_receipt_count": manifest.state.attempt_receipt_count,
                "pending_attempt_count": manifest.state.pending_attempt_count,
                "attempt_receipt_identity_sha256": (manifest.state.attempt_receipt_identity_sha256),
                "live_pin_count": manifest.state.live_pin_count,
                "live_pin_identity_sha256": manifest.state.live_pin_identity_sha256,
                "rolled_up_routed_request_count": (manifest.state.rolled_up_routed_request_count),
                "admin_ledger_state_sha256": manifest.state.admin_ledger_state_sha256,
            }
        )
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _verify_vault_key(vault_key: Path, state_path: Path) -> DatabaseStateValue:
    _assert_root_regular(vault_key, maximum_bytes=VAULT_KEY_BYTES)
    state = _load_state(state_path)
    verify_vault_key_material(vault_key.read_bytes(), state)
    return state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    _ = create.add_argument("--backup-id", required=True)
    _ = create.add_argument("--created-at", required=True)
    _ = create.add_argument("--database-dump", type=Path, required=True)
    _ = create.add_argument("--vault-key", type=Path, required=True)
    _ = create.add_argument("--state", type=Path, required=True)
    _ = create.add_argument("--manifest", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    _ = verify.add_argument("--database-dump", type=Path, required=True)
    _ = verify.add_argument("--vault-key", type=Path, required=True)
    _ = verify.add_argument("--manifest", type=Path, required=True)
    compare = subparsers.add_parser("compare-state")
    _ = compare.add_argument("--state", type=Path, required=True)
    _ = compare.add_argument("--manifest", type=Path, required=True)
    verify_key = subparsers.add_parser("verify-vault-key")
    _ = verify_key.add_argument("--vault-key", type=Path, required=True)
    _ = verify_key.add_argument("--state", type=Path, required=True)
    return parser


def _run(arguments: _Arguments) -> str:
    if arguments.command == "create":
        _assert_root_directory(arguments.database_dump.parent)
        _assert_root_directory(arguments.vault_key.parent)
        _assert_root_regular(arguments.database_dump)
        _assert_root_regular(arguments.vault_key)
        manifest = build_manifest(
            backup_id=arguments.backup_id,
            created_at=arguments.created_at,
            database_dump=arguments.database_dump,
            vault_key=arguments.vault_key,
            state=_load_state(arguments.state),
        )
        _write_manifest(arguments.manifest, manifest)
        return _safe_receipt(manifest, restored=False)
    if arguments.command == "verify-vault-key":
        state = _verify_vault_key(arguments.vault_key, arguments.state)
        return json.dumps(
            {
                "schema_version": state.schema_version,
                "status": "PASS",
                "vault_key_matches_database": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    _assert_root_directory(arguments.manifest.parent)
    manifest = _load_manifest(arguments.manifest)
    if arguments.command == "verify":
        _assert_root_directory(arguments.database_dump.parent)
        _assert_root_directory(arguments.vault_key.parent)
        _assert_root_regular(arguments.database_dump)
        _assert_root_regular(arguments.vault_key)
        verify_manifest(
            manifest,
            database_dump=arguments.database_dump,
            vault_key=arguments.vault_key,
        )
        return _safe_receipt(manifest, restored=False)
    if arguments.command == "compare-state":
        verify_restored_state(manifest.state, _load_state(arguments.state))
        return _safe_receipt(manifest, restored=True)
    reason = "command_invalid"
    raise BackupContractError(reason)


def main() -> None:
    """Run value-silent root-artifact creation or verification."""
    status = 0
    try:
        output = _run(_parser().parse_args(namespace=_Arguments()))
        _ = sys.stdout.write(f"{output}\n")
    except BackupContractError as error:
        status = 1
        _ = sys.stderr.write(f"backup_contract_failed:{error}\n")
    except BaseException:  # noqa: BLE001 - process boundary hides unclassified details.
        status = 1
        _ = sys.stderr.write("backup_contract_failed\n")
    raise SystemExit(status)
