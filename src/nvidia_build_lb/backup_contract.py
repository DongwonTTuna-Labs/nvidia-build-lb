"""Stable public and module-CLI surface for the paired backup contract."""

from nvidia_build_lb.backup_cli import main
from nvidia_build_lb.backup_manifest import (
    build_manifest,
    state_mismatch_fields,
    verify_manifest,
    verify_restored_state,
)
from nvidia_build_lb.backup_models import (
    BackupContractError,
    BackupManifest,
    BackupManifestV2,
    BackupManifestV3,
    DatabaseState,
    DatabaseStateV2,
    DatabaseStateV3,
    parse_backup_manifest_json,
    parse_database_state_json,
)

__all__ = [
    "BackupContractError",
    "BackupManifest",
    "BackupManifestV2",
    "BackupManifestV3",
    "DatabaseState",
    "DatabaseStateV2",
    "DatabaseStateV3",
    "build_manifest",
    "parse_backup_manifest_json",
    "parse_database_state_json",
    "state_mismatch_fields",
    "verify_manifest",
    "verify_restored_state",
]


if __name__ == "__main__":
    main()
