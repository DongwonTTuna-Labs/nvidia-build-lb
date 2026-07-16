"""Version-dispatched backup state and exact 0005 evidence contracts."""

from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretBytes

from nvidia_build_lb.backup_contract import (
    BackupContractError,
    BackupManifestV3,
    DatabaseStateV2,
    DatabaseStateV3,
    build_manifest,
    parse_backup_manifest_json,
    parse_database_state_json,
    verify_restored_state,
)
from nvidia_build_lb.vault import Vault

_UPSTREAM_ID = UUID("00000000-0000-4000-8000-000000000001")
_VAULT_KEY = b"v" * 32
_VERIFIER = Vault(SecretBytes(_VAULT_KEY)).build_key_verifier()


def _v3_payload() -> dict[str, object]:
    return {
        "schema_version": 3,
        "alembic_revision": "0005_admin_dashboard_ledger",
        "vault_verifier_salt": _VERIFIER.salt.hex(),
        "vault_verifier_digest": _VERIFIER.digest.hex(),
        "upstream_count": 1,
        "upstream_identity_sha256": "1" * 64,
        "upstream_keys": [{"id": str(_UPSTREAM_ID), "fingerprint": f"sha256:{'2' * 64}"}],
        "downstream_count": 1,
        "downstream_digest_sha256": "3" * 64,
        "admin_event_count": 4,
        "admin_event_identity_sha256": "4" * 64,
        "attempt_receipt_count": 2,
        "pending_attempt_count": 1,
        "attempt_receipt_identity_sha256": "5" * 64,
        "live_pin_count": 1,
        "live_pin_identity_sha256": "6" * 64,
        "rolled_up_routed_request_count": 7,
        "admin_ledger_state_sha256": "7" * 64,
    }


def _v3(**updates: object) -> DatabaseStateV3:
    return DatabaseStateV3.model_validate(_v3_payload() | updates)


def test_v3_receipt_identity_hash_mismatch_fails_restore_compare() -> None:
    observed = _v3(attempt_receipt_identity_sha256="8" * 64)

    with pytest.raises(
        BackupContractError,
        match="restored_state_mismatch_attempt_receipt_identity_sha256",
    ):
        verify_restored_state(_v3(), observed)


def test_v3_live_pin_and_ledger_hash_mismatch_fail_restore_compare() -> None:
    mismatches = (
        ("live_pin_identity_sha256", "8" * 64),
        ("admin_ledger_state_sha256", "9" * 64),
    )
    for field, digest in mismatches:
        with pytest.raises(BackupContractError, match=f"restored_state_mismatch_{field}"):
            verify_restored_state(_v3(), _v3(**{field: digest}))


def test_state_parser_dispatches_strict_v2_and_v3_field_sets() -> None:
    v2_payload = {
        key: value
        for key, value in _v3_payload().items()
        if key
        not in {
            "admin_event_count",
            "admin_event_identity_sha256",
            "attempt_receipt_count",
            "pending_attempt_count",
            "attempt_receipt_identity_sha256",
            "live_pin_count",
            "live_pin_identity_sha256",
            "rolled_up_routed_request_count",
            "admin_ledger_state_sha256",
        }
    } | {"schema_version": 2, "alembic_revision": "0004_vault_key_verifier"}

    parsed_v2 = parse_database_state_json(
        DatabaseStateV2.model_validate(v2_payload).model_dump_json()
    )
    assert isinstance(parsed_v2, DatabaseStateV2)
    assert isinstance(parse_database_state_json(_v3().model_dump_json()), DatabaseStateV3)
    with pytest.raises(ValueError, match="live_pin_count"):
        _ = parse_database_state_json(_v3().model_dump_json(exclude={"live_pin_count"}))


def test_v3_manifest_round_trip_binds_v3_state_and_pair_id(tmp_path: Path) -> None:
    database_dump = tmp_path / "database.dump"
    vault_key = tmp_path / "vault_master_key"
    _ = database_dump.write_bytes(b"PGDMP\x00v3-fixture")
    _ = vault_key.write_bytes(_VAULT_KEY)

    manifest = build_manifest(
        backup_id="backup-v3-20260715",
        created_at="2026-07-15T00:00:00Z",
        database_dump=database_dump,
        vault_key=vault_key,
        state=_v3(),
    )
    parsed = parse_backup_manifest_json(manifest.model_dump_json())

    assert isinstance(manifest, BackupManifestV3)
    assert isinstance(parsed, BackupManifestV3)
    assert parsed == manifest
    assert parsed.state.attempt_receipt_count == 2
