"""Root-only paired database and vault-key backup manifest contract."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    StringConstraints,
    model_validator,
)

from nvidia_build_lb.vault import Vault, VaultKeyVerifier

_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_FINGERPRINT = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
_BACKUP_ID = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
]
_UTC_TIMESTAMP = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
]
_MAX_STATE_BYTES = 1024 * 1024
_VAULT_KEY_BYTES = 32
_PG_DUMP_MAGIC = b"PGDMP"
_ROOT_FILE_MODE = 0o600
_ROOT_DIRECTORY_MODE = 0o700


class BackupContractError(RuntimeError):
    """One stable non-sensitive backup contract failure."""


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class UpstreamIdentity(_StrictModel):
    """Safe upstream identity retained across backup and restore."""

    id: UUID4
    fingerprint: _FINGERPRINT


class DatabaseState(_StrictModel):
    """Secret-safe exact database identity compared after isolated restore."""

    schema_version: Literal[2] = 2
    alembic_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-z_]{1,64}$")]
    vault_verifier_salt: _SHA256
    vault_verifier_digest: _SHA256
    upstream_count: int = Field(ge=0)
    upstream_identity_sha256: _SHA256
    upstream_keys: tuple[UpstreamIdentity, ...]
    downstream_count: int = Field(ge=0)
    downstream_digest_sha256: _SHA256

    @model_validator(mode="after")
    def validate_upstream_identities(self) -> Self:
        """Bind the count and reject duplicate or noncanonical identity order."""
        if self.upstream_count != len(self.upstream_keys):
            message = "upstream count does not match identities"
            raise ValueError(message)
        canonical = tuple(sorted(self.upstream_keys, key=lambda item: str(item.id)))
        if canonical != self.upstream_keys:
            message = "upstream identities are not canonically ordered"
            raise ValueError(message)
        if len({item.id for item in self.upstream_keys}) != len(self.upstream_keys):
            message = "upstream identities must be unique"
            raise ValueError(message)
        return self


class ArtifactDigest(_StrictModel):
    """Filename, length, and digest of one root-only artifact."""

    filename: Literal["database.dump", "vault_master_key"]
    size_bytes: int = Field(gt=0)
    sha256: _SHA256


class BackupManifest(_StrictModel):
    """Exact two-part backup tuple and safe restored-state oracle."""

    schema_version: Literal[2] = 2
    backup_id: _BACKUP_ID
    created_at: _UTC_TIMESTAMP
    pair_id: _SHA256
    database: ArtifactDigest
    vault_master_key: ArtifactDigest
    state: DatabaseState

    @model_validator(mode="after")
    def validate_artifact_names(self) -> Self:
        """Reject swapped or aliased halves even if their digests are valid."""
        if self.database.filename != "database.dump":
            message = "database artifact name is invalid"
            raise ValueError(message)
        if self.vault_master_key.filename != "vault_master_key":
            message = "vault artifact name is invalid"
            raise ValueError(message)
        return self


class _Arguments(argparse.Namespace):
    command: str = ""
    backup_id: str = ""
    created_at: str = ""
    database_dump: Path = Path()
    vault_key: Path = Path()
    state: Path = Path()
    manifest: Path = Path()


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _pair_id(
    *,
    backup_id: str,
    database: ArtifactDigest,
    vault_master_key: ArtifactDigest,
    state: DatabaseState,
) -> str:
    payload = {
        "backup_id": backup_id,
        "database": database.model_dump(mode="json"),
        "state": state.model_dump(mode="json"),
        "vault_master_key": vault_master_key.model_dump(mode="json"),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def build_manifest(
    *,
    backup_id: str,
    created_at: str,
    database_dump: Path,
    vault_key: Path,
    state: DatabaseState,
) -> BackupManifest:
    """Build a manifest without embedding either artifact payload."""
    database_size, database_sha256 = _digest(database_dump)
    key_material = vault_key.read_bytes()
    key_size = len(key_material)
    key_sha256 = hashlib.sha256(key_material).hexdigest()
    if database_size <= len(_PG_DUMP_MAGIC):
        reason = "database_dump_invalid"
        raise BackupContractError(reason)
    with database_dump.open("rb") as stream:
        if stream.read(len(_PG_DUMP_MAGIC)) != _PG_DUMP_MAGIC:
            reason = "database_dump_invalid"
            raise BackupContractError(reason)
    if key_size != _VAULT_KEY_BYTES:
        reason = "vault_key_invalid"
        raise BackupContractError(reason)
    _verify_vault_key_material_against_state(key_material, state)
    database = ArtifactDigest(
        filename="database.dump",
        size_bytes=database_size,
        sha256=database_sha256,
    )
    vault_master_key = ArtifactDigest(
        filename="vault_master_key",
        size_bytes=key_size,
        sha256=key_sha256,
    )
    pair_id = _pair_id(
        backup_id=backup_id,
        database=database,
        vault_master_key=vault_master_key,
        state=state,
    )
    return BackupManifest(
        backup_id=backup_id,
        created_at=created_at,
        pair_id=pair_id,
        database=database,
        vault_master_key=vault_master_key,
        state=state,
    )


def verify_manifest(
    manifest: BackupManifest,
    *,
    database_dump: Path,
    vault_key: Path,
) -> None:
    """Rehash both artifact halves and the safe manifest tuple."""
    observed = build_manifest(
        backup_id=manifest.backup_id,
        created_at=manifest.created_at,
        database_dump=database_dump,
        vault_key=vault_key,
        state=manifest.state,
    )
    if observed != manifest:
        reason = "artifact_mismatch"
        raise BackupContractError(reason)


def state_mismatch_fields(expected: DatabaseState, observed: DatabaseState) -> tuple[str, ...]:
    """Return only safe field names whose restored values differ."""
    fields = (
        "alembic_revision",
        "vault_verifier_salt",
        "vault_verifier_digest",
        "upstream_count",
        "upstream_identity_sha256",
        "upstream_keys",
        "downstream_count",
        "downstream_digest_sha256",
    )
    return tuple(field for field in fields if getattr(expected, field) != getattr(observed, field))


def _assert_root_regular(path: Path, *, maximum_bytes: int | None = None) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != _ROOT_FILE_MODE
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
        or stat.S_IMODE(metadata.st_mode) != _ROOT_DIRECTORY_MODE
    ):
        reason = "root_directory_invalid"
        raise BackupContractError(reason)


def _load_state(path: Path) -> DatabaseState:
    _assert_root_regular(path, maximum_bytes=_MAX_STATE_BYTES)
    return DatabaseState.model_validate_json(path.read_bytes())


def _load_manifest(path: Path) -> BackupManifest:
    _assert_root_regular(path, maximum_bytes=_MAX_STATE_BYTES)
    return BackupManifest.model_validate_json(path.read_bytes())


def _write_manifest(path: Path, manifest: BackupManifest) -> None:
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
        payload = (manifest.model_dump_json(indent=2) + "\n").encode("utf-8")
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


def _safe_receipt(manifest: BackupManifest, *, restored: bool) -> str:
    payload = {
        "schema_version": 2,
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
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _verify_vault_key_material_against_state(
    key_material: bytes,
    state: DatabaseState,
) -> None:
    """Prove one in-memory key snapshot matches the captured DB verifier."""
    if len(key_material) != _VAULT_KEY_BYTES:
        reason = "vault_key_invalid"
        raise BackupContractError(reason)
    try:
        verifier = VaultKeyVerifier(
            salt=bytes.fromhex(state.vault_verifier_salt),
            digest=bytes.fromhex(state.vault_verifier_digest),
        )
        vault = Vault(SecretBytes(key_material))
    except ValueError:
        reason = "vault_key_database_mismatch"
        raise BackupContractError(reason) from None
    if not vault.matches_key_verifier(verifier):
        reason = "vault_key_database_mismatch"
        raise BackupContractError(reason)


def _verify_vault_key_against_state(vault_key: Path, state_path: Path) -> None:
    """Prove a root-only key matches the database verifier without printing it."""
    _assert_root_regular(vault_key, maximum_bytes=_VAULT_KEY_BYTES)
    state = _load_state(state_path)
    _verify_vault_key_material_against_state(vault_key.read_bytes(), state)


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
        state = _load_state(arguments.state)
        manifest = build_manifest(
            backup_id=arguments.backup_id,
            created_at=arguments.created_at,
            database_dump=arguments.database_dump,
            vault_key=arguments.vault_key,
            state=state,
        )
        _write_manifest(arguments.manifest, manifest)
        return _safe_receipt(manifest, restored=False)
    if arguments.command == "verify-vault-key":
        _verify_vault_key_against_state(arguments.vault_key, arguments.state)
        return json.dumps(
            {
                "schema_version": 2,
                "status": "PASS",
                "vault_key_matches_database": True,
            },
            ensure_ascii=True,
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
        state = _load_state(arguments.state)
        mismatches = state_mismatch_fields(manifest.state, state)
        if mismatches:
            reason = "restored_state_mismatch_" + "_".join(mismatches)
            raise BackupContractError(reason)
        return _safe_receipt(manifest, restored=True)
    reason = "command_invalid"
    raise BackupContractError(reason)


def main() -> None:
    """Run value-silent root-artifact creation or verification."""
    status = 0
    try:
        arguments = _parser().parse_args(namespace=_Arguments())
        output = _run(arguments)
        _ = sys.stdout.write(f"{output}\n")
    except BackupContractError as error:
        status = 1
        _ = sys.stderr.write(f"backup_contract_failed:{error}\n")
    except BaseException:  # noqa: BLE001 - process boundary hides unclassified details.
        status = 1
        _ = sys.stderr.write("backup_contract_failed\n")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
