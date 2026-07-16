"""Strict secret-safe models shared by backup creation and restore verification."""

from typing import Annotated, ClassVar, Literal, Self

from pydantic import (
    UUID4,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    model_validator,
)

SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
FINGERPRINT = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
BACKUP_ID = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
UTC_TIMESTAMP = Annotated[
    str,
    StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"),
]
MAX_STATE_BYTES = 1024 * 1024
VAULT_KEY_BYTES = 32
PG_DUMP_MAGIC = b"PGDMP"
ROOT_FILE_MODE = 0o600
ROOT_DIRECTORY_MODE = 0o700


class BackupContractError(RuntimeError):
    """One stable non-sensitive backup contract failure."""


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class UpstreamIdentity(_StrictModel):
    """Safe upstream identity retained across backup and restore."""

    id: UUID4
    fingerprint: FINGERPRINT


class _DatabaseStateBase(_StrictModel):
    alembic_revision: Annotated[str, StringConstraints(pattern=r"^[0-9a-z_]{1,64}$")]
    vault_verifier_salt: SHA256
    vault_verifier_digest: SHA256
    upstream_count: int = Field(ge=0)
    upstream_identity_sha256: SHA256
    upstream_keys: tuple[UpstreamIdentity, ...]
    downstream_count: int = Field(ge=0)
    downstream_digest_sha256: SHA256

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


class DatabaseStateV2(_DatabaseStateBase):
    """Exact secret-safe identity for the pre-0005 database schema."""

    schema_version: Literal[2] = 2


class DatabaseStateV3(_DatabaseStateBase):
    """Exact secret-safe identity including every 0005 evidence-bearing row."""

    schema_version: Literal[3] = 3
    admin_event_count: int = Field(ge=0)
    admin_event_identity_sha256: SHA256
    attempt_receipt_count: int = Field(ge=0)
    pending_attempt_count: int = Field(ge=0)
    attempt_receipt_identity_sha256: SHA256
    live_pin_count: int = Field(ge=0)
    live_pin_identity_sha256: SHA256
    rolled_up_routed_request_count: int = Field(ge=0)
    admin_ledger_state_sha256: SHA256

    @model_validator(mode="after")
    def validate_attempt_counts(self) -> Self:
        """Reject impossible pending or live-pin counts before manifest creation."""
        if self.pending_attempt_count > self.attempt_receipt_count:
            message = "pending attempt count exceeds receipt count"
            raise ValueError(message)
        if self.live_pin_count > self.attempt_receipt_count:
            message = "live pin count exceeds receipt count"
            raise ValueError(message)
        return self


type DatabaseStateValue = Annotated[
    DatabaseStateV2 | DatabaseStateV3,
    Field(discriminator="schema_version"),
]
DatabaseState = DatabaseStateV2
_DATABASE_STATE_ADAPTER: TypeAdapter[DatabaseStateV2 | DatabaseStateV3] = TypeAdapter(
    DatabaseStateValue
)


class ArtifactDigest(_StrictModel):
    """Filename, length, and digest of one root-only artifact."""

    filename: Literal["database.dump", "vault_master_key"]
    size_bytes: int = Field(gt=0)
    sha256: SHA256


class _BackupManifestBase(_StrictModel):
    backup_id: BACKUP_ID
    created_at: UTC_TIMESTAMP
    pair_id: SHA256
    database: ArtifactDigest
    vault_master_key: ArtifactDigest

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


class BackupManifestV2(_BackupManifestBase):
    """Strict paired-backup manifest for a V2 database state."""

    schema_version: Literal[2] = 2
    state: DatabaseStateV2


class BackupManifestV3(_BackupManifestBase):
    """Strict paired-backup manifest for a V3 database state."""

    schema_version: Literal[3] = 3
    state: DatabaseStateV3


type BackupManifestValue = Annotated[
    BackupManifestV2 | BackupManifestV3,
    Field(discriminator="schema_version"),
]
BackupManifest = BackupManifestV2
_BACKUP_MANIFEST_ADAPTER: TypeAdapter[BackupManifestV2 | BackupManifestV3] = TypeAdapter(
    BackupManifestValue
)


def parse_database_state_json(payload: bytes | str) -> DatabaseStateValue:
    """Strictly dispatch a serialized database state by schema version."""
    return _DATABASE_STATE_ADAPTER.validate_json(payload)


def parse_backup_manifest_json(payload: bytes | str) -> BackupManifestValue:
    """Strictly dispatch a serialized paired-backup manifest by schema version."""
    return _BACKUP_MANIFEST_ADAPTER.validate_json(payload)
