"""Manifest construction and vault/database binding for paired backups."""

import hashlib
import json
from pathlib import Path

from pydantic import SecretBytes

from nvidia_build_lb.backup_models import (
    PG_DUMP_MAGIC,
    VAULT_KEY_BYTES,
    ArtifactDigest,
    BackupContractError,
    BackupManifestV2,
    BackupManifestV3,
    BackupManifestValue,
    DatabaseStateV3,
    DatabaseStateValue,
)
from nvidia_build_lb.vault import Vault, VaultKeyVerifier


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
    state: DatabaseStateValue,
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


def verify_vault_key_material(key_material: bytes, state: DatabaseStateValue) -> None:
    """Prove one in-memory key snapshot matches the captured DB verifier."""
    if len(key_material) != VAULT_KEY_BYTES:
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


def build_manifest(
    *,
    backup_id: str,
    created_at: str,
    database_dump: Path,
    vault_key: Path,
    state: DatabaseStateValue,
) -> BackupManifestValue:
    """Build a manifest without embedding either artifact payload."""
    database_size, database_sha256 = _digest(database_dump)
    key_material = vault_key.read_bytes()
    key_size = len(key_material)
    key_sha256 = hashlib.sha256(key_material).hexdigest()
    if database_size <= len(PG_DUMP_MAGIC):
        reason = "database_dump_invalid"
        raise BackupContractError(reason)
    with database_dump.open("rb") as stream:
        if stream.read(len(PG_DUMP_MAGIC)) != PG_DUMP_MAGIC:
            reason = "database_dump_invalid"
            raise BackupContractError(reason)
    if key_size != VAULT_KEY_BYTES:
        reason = "vault_key_invalid"
        raise BackupContractError(reason)
    verify_vault_key_material(key_material, state)
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
    if isinstance(state, DatabaseStateV3):
        return BackupManifestV3(
            backup_id=backup_id,
            created_at=created_at,
            pair_id=pair_id,
            database=database,
            vault_master_key=vault_master_key,
            state=state,
        )
    return BackupManifestV2(
        backup_id=backup_id,
        created_at=created_at,
        pair_id=pair_id,
        database=database,
        vault_master_key=vault_master_key,
        state=state,
    )


def verify_manifest(
    manifest: BackupManifestValue,
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


def state_mismatch_fields(
    expected: DatabaseStateValue,
    observed: DatabaseStateValue,
) -> tuple[str, ...]:
    """Return only safe field names whose restored values differ."""
    if expected.schema_version != observed.schema_version:
        return ("schema_version",)
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
    if isinstance(expected, DatabaseStateV3) and isinstance(observed, DatabaseStateV3):
        fields += (
            "admin_event_count",
            "admin_event_identity_sha256",
            "attempt_receipt_count",
            "pending_attempt_count",
            "attempt_receipt_identity_sha256",
            "live_pin_count",
            "live_pin_identity_sha256",
            "rolled_up_routed_request_count",
            "admin_ledger_state_sha256",
        )
    return tuple(field for field in fields if getattr(expected, field) != getattr(observed, field))


def verify_restored_state(
    expected: DatabaseStateValue,
    observed: DatabaseStateValue,
) -> None:
    """Reject any version-specific restored database state mismatch."""
    mismatches = state_mismatch_fields(expected, observed)
    if mismatches:
        reason = "restored_state_mismatch_" + "_".join(mismatches)
        raise BackupContractError(reason)
