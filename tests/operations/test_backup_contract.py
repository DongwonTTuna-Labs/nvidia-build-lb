"""Root-only paired backup manifest contracts."""

from pathlib import Path
from uuid import UUID

import pytest

from nvidia_build_lb.backup_contract import (
    BackupContractError,
    DatabaseState,
    build_manifest,
    state_mismatch_fields,
    verify_manifest,
)

_UPSTREAM_ID = UUID("00000000-0000-4000-8000-000000000001")
_UPSTREAM_FINGERPRINT = f"sha256:{'1' * 64}"


def _state() -> DatabaseState:
    return DatabaseState.model_validate(
        {
            "schema_version": 1,
            "alembic_revision": "0003_nvidia_routing",
            "upstream_count": 1,
            "upstream_identity_sha256": "2" * 64,
            "upstream_keys": [{"id": str(_UPSTREAM_ID), "fingerprint": _UPSTREAM_FINGERPRINT}],
            "downstream_count": 1,
            "downstream_digest_sha256": "3" * 64,
        }
    )


def _state_payload(
    *,
    upstream_count: int = 1,
    duplicate: bool = False,
) -> dict[str, object]:
    identity: dict[str, str] = {
        "id": str(_UPSTREAM_ID),
        "fingerprint": _UPSTREAM_FINGERPRINT,
    }
    identities = [identity, identity.copy()] if duplicate else [identity]
    return {
        "schema_version": 1,
        "alembic_revision": "0003_nvidia_routing",
        "upstream_count": upstream_count,
        "upstream_identity_sha256": "2" * 64,
        "upstream_keys": identities,
        "downstream_count": 1,
        "downstream_digest_sha256": "3" * 64,
    }


def test_manifest_binds_separate_dump_key_and_safe_database_identity(tmp_path: Path) -> None:
    database_dump = tmp_path / "database.dump"
    vault_key = tmp_path / "vault_master_key"
    _ = database_dump.write_bytes(b"PGDMP\x00fixture")
    _ = vault_key.write_bytes(b"k" * 32)

    manifest = build_manifest(
        backup_id="backup-20260714t000000z",
        created_at="2026-07-14T00:00:00Z",
        database_dump=database_dump,
        vault_key=vault_key,
        state=_state(),
    )

    assert manifest.database.filename == "database.dump"
    assert manifest.vault_master_key.filename == "vault_master_key"
    assert manifest.database.sha256 != manifest.vault_master_key.sha256
    assert manifest.state.upstream_keys[0].id == _UPSTREAM_ID
    assert manifest.state.upstream_keys[0].fingerprint == _UPSTREAM_FINGERPRINT
    assert len(manifest.pair_id) == 64
    verify_manifest(manifest, database_dump=database_dump, vault_key=vault_key)


def test_manifest_verification_fails_if_either_half_changes(tmp_path: Path) -> None:
    database_dump = tmp_path / "database.dump"
    vault_key = tmp_path / "vault_master_key"
    _ = database_dump.write_bytes(b"PGDMP\x00fixture")
    _ = vault_key.write_bytes(b"k" * 32)
    manifest = build_manifest(
        backup_id="backup-20260714t000000z",
        created_at="2026-07-14T00:00:00Z",
        database_dump=database_dump,
        vault_key=vault_key,
        state=_state(),
    )

    _ = database_dump.write_bytes(b"PGDMP\x00changed")
    with pytest.raises(BackupContractError, match="artifact_mismatch"):
        verify_manifest(manifest, database_dump=database_dump, vault_key=vault_key)

    _ = database_dump.write_bytes(b"PGDMP\x00fixture")
    _ = vault_key.write_bytes(b"z" * 32)
    with pytest.raises(BackupContractError, match="artifact_mismatch"):
        verify_manifest(manifest, database_dump=database_dump, vault_key=vault_key)


def test_database_state_rejects_count_or_identity_drift() -> None:
    with pytest.raises(ValueError, match="upstream count"):
        _ = DatabaseState.model_validate(_state_payload(upstream_count=2))

    with pytest.raises(ValueError, match="unique"):
        _ = DatabaseState.model_validate(_state_payload(upstream_count=2, duplicate=True))


def test_state_mismatch_diagnostic_contains_only_safe_field_names() -> None:
    observed = DatabaseState.model_validate(
        _state_payload(upstream_count=0) | {"upstream_keys": []}
    )
    assert state_mismatch_fields(_state(), observed) == (
        "upstream_count",
        "upstream_keys",
    )
