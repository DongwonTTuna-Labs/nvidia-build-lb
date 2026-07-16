"""Fail-closed runtime settings loaded from secret file paths."""

from enum import StrEnum
from pathlib import Path
from typing import Annotated, ClassVar, Final

from pydantic import BaseModel, ConfigDict, Field, SecretBytes, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from nvidia_build_lb.errors import ConfigurationError, ConfigurationErrorCode
from nvidia_build_lb.routing_limits import MAX_PUBLIC_ATTEMPTS

NVIDIA_BASE_URL: Final = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL: Final = "z-ai/glm-5.2"
VAULT_KEY_BYTES: Final = 32
_ADMIN_TOKEN_PREFIX: Final = b"nblb_admin_"
_ADMIN_TOKEN_HEX_BYTES: Final = 64
_ADMIN_TOKEN_BYTES: Final = len(_ADMIN_TOKEN_PREFIX) + _ADMIN_TOKEN_HEX_BYTES
_DATABASE_PASSWORD_MAX_BYTES: Final = 1024
_MAX_DECIMAL_SETTING_DIGITS: Final = 10

type AdminReadDeadlineSeconds = Annotated[int, Field(ge=1, le=5, strict=True)]
type AdminMutationDeadlineSeconds = Annotated[int, Field(ge=5, le=125, strict=True)]
type AdminEventRetentionDays = Annotated[int, Field(ge=1, le=365, strict=True)]
type AdminEventMaxRows = Annotated[int, Field(ge=1_000, le=1_000_000, strict=True)]
type AdminAttemptMaxRows = Annotated[int, Field(ge=100, le=400_000, strict=True)]
type AdminLedgerPruneBatchSize = Annotated[int, Field(ge=6, le=5_000, strict=True)]
type AdminLedgerMaintenanceIntervalSeconds = Annotated[int, Field(ge=10, le=3_600, strict=True)]
type AdminAttemptReconciliationGraceSeconds = Annotated[
    int,
    Field(ge=300, le=3_600, strict=True),
]


class DeploymentStage(StrEnum):
    """Supported log rendering stages."""

    DEVELOPMENT = "development"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    """Supported service log thresholds."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class SettingsSource(BaseSettings):
    """Non-secret values and paths accepted at the process boundary."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="NVIDIA_BUILD_LB_",
        extra="forbid",
        frozen=True,
    )

    database_url: SecretStr = SecretStr("postgresql+asyncpg://nvidia_build_lb@db/nvidia_build_lb")
    database_password_file: Path | None = None
    vault_key_file: Path = Path("/run/nvidia-build-lb/secrets/vault_master_key")
    admin_token_file: Path = Path("/run/nvidia-build-lb/secrets/admin_token")
    stage: str = DeploymentStage.PRODUCTION.value
    log_level: str = LogLevel.INFO.value
    admin_read_deadline_seconds: AdminReadDeadlineSeconds = 5
    admin_mutation_deadline_seconds: AdminMutationDeadlineSeconds = 125
    admin_event_retention_days: AdminEventRetentionDays = 30
    admin_event_max_rows: AdminEventMaxRows = 100_000
    admin_attempt_max_rows: AdminAttemptMaxRows = 40_000
    admin_ledger_prune_batch_size: AdminLedgerPruneBatchSize = 1_000
    admin_ledger_maintenance_interval_seconds: AdminLedgerMaintenanceIntervalSeconds = 300
    admin_attempt_reconciliation_grace_seconds: AdminAttemptReconciliationGraceSeconds = 300

    @field_validator(
        "admin_event_max_rows",
        "admin_attempt_max_rows",
        "admin_ledger_prune_batch_size",
        mode="before",
    )
    @classmethod
    def _parse_compose_integer(cls, value: object) -> object:
        """Parse canonical decimal strings emitted by Compose."""
        if (
            isinstance(value, str)
            and len(value) <= _MAX_DECIMAL_SETTING_DIGITS
            and not value.startswith("0")
            and value.isascii()
            and value.isdecimal()
        ):
            return int(value)
        return value


class SettingsMetadata(BaseModel):
    """The complete safe-to-serialize settings view."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    base_url: str
    model: str
    stage: DeploymentStage
    log_level: LogLevel
    secret_files_loaded: bool
    admin_read_deadline_seconds: AdminReadDeadlineSeconds
    admin_mutation_deadline_seconds: AdminMutationDeadlineSeconds
    admin_event_retention_days: AdminEventRetentionDays
    admin_event_max_rows: AdminEventMaxRows
    admin_attempt_max_rows: AdminAttemptMaxRows
    admin_ledger_prune_batch_size: AdminLedgerPruneBatchSize
    admin_ledger_maintenance_interval_seconds: AdminLedgerMaintenanceIntervalSeconds
    admin_attempt_reconciliation_grace_seconds: AdminAttemptReconciliationGraceSeconds


class Settings(BaseModel):
    """Parsed runtime settings with secret values masked by construction."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    database_url: SecretStr
    vault_key: SecretBytes
    admin_token: SecretStr
    stage: DeploymentStage
    log_level: LogLevel
    admin_read_deadline_seconds: AdminReadDeadlineSeconds
    admin_mutation_deadline_seconds: AdminMutationDeadlineSeconds
    admin_event_retention_days: AdminEventRetentionDays
    admin_event_max_rows: AdminEventMaxRows
    admin_attempt_max_rows: AdminAttemptMaxRows
    admin_ledger_prune_batch_size: AdminLedgerPruneBatchSize
    admin_ledger_maintenance_interval_seconds: AdminLedgerMaintenanceIntervalSeconds
    admin_attempt_reconciliation_grace_seconds: AdminAttemptReconciliationGraceSeconds

    def safe_metadata(self) -> SettingsMetadata:
        """Return the only settings representation permitted in evidence."""
        return SettingsMetadata(
            base_url=NVIDIA_BASE_URL,
            model=NVIDIA_MODEL,
            stage=self.stage,
            log_level=self.log_level,
            secret_files_loaded=True,
            admin_read_deadline_seconds=self.admin_read_deadline_seconds,
            admin_mutation_deadline_seconds=self.admin_mutation_deadline_seconds,
            admin_event_retention_days=self.admin_event_retention_days,
            admin_event_max_rows=self.admin_event_max_rows,
            admin_attempt_max_rows=self.admin_attempt_max_rows,
            admin_ledger_prune_batch_size=self.admin_ledger_prune_batch_size,
            admin_ledger_maintenance_interval_seconds=(
                self.admin_ledger_maintenance_interval_seconds
            ),
            admin_attempt_reconciliation_grace_seconds=(
                self.admin_attempt_reconciliation_grace_seconds
            ),
        )


def load_settings(source: SettingsSource | None = None) -> Settings:
    """Parse process settings and fail closed without retaining rejected values."""
    boundary = source if source is not None else SettingsSource()

    database_url = load_database_url(boundary)

    try:
        stage = DeploymentStage(boundary.stage)
    except ValueError:
        raise ConfigurationError(code=ConfigurationErrorCode.STAGE_INVALID) from None
    try:
        log_level = LogLevel(boundary.log_level)
    except ValueError:
        raise ConfigurationError(code=ConfigurationErrorCode.LOG_LEVEL_INVALID) from None

    if (
        2 * boundary.admin_attempt_max_rows + 100 > boundary.admin_event_max_rows
        or boundary.admin_ledger_prune_batch_size < 3 * MAX_PUBLIC_ATTEMPTS
    ):
        raise ConfigurationError(code=ConfigurationErrorCode.ADMIN_LEDGER_INVALID)

    try:
        vault_key = boundary.vault_key_file.read_bytes()
    except OSError:
        raise ConfigurationError(code=ConfigurationErrorCode.VAULT_KEY_UNAVAILABLE) from None
    if len(vault_key) != VAULT_KEY_BYTES:
        raise ConfigurationError(code=ConfigurationErrorCode.VAULT_KEY_INVALID)

    try:
        admin_token_bytes = boundary.admin_token_file.read_bytes()
    except OSError:
        raise ConfigurationError(code=ConfigurationErrorCode.ADMIN_TOKEN_UNAVAILABLE) from None
    admin_token_bytes = admin_token_bytes.removesuffix(b"\n")
    if (
        len(admin_token_bytes) != _ADMIN_TOKEN_BYTES
        or not admin_token_bytes.startswith(_ADMIN_TOKEN_PREFIX)
        or any(
            byte not in b"0123456789abcdef"
            for byte in admin_token_bytes[len(_ADMIN_TOKEN_PREFIX) :]
        )
    ):
        raise ConfigurationError(code=ConfigurationErrorCode.ADMIN_TOKEN_INVALID)

    return Settings(
        database_url=database_url,
        vault_key=SecretBytes(vault_key),
        admin_token=SecretStr(admin_token_bytes.decode("ascii")),
        stage=stage,
        log_level=log_level,
        admin_read_deadline_seconds=boundary.admin_read_deadline_seconds,
        admin_mutation_deadline_seconds=boundary.admin_mutation_deadline_seconds,
        admin_event_retention_days=boundary.admin_event_retention_days,
        admin_event_max_rows=boundary.admin_event_max_rows,
        admin_attempt_max_rows=boundary.admin_attempt_max_rows,
        admin_ledger_prune_batch_size=boundary.admin_ledger_prune_batch_size,
        admin_ledger_maintenance_interval_seconds=(
            boundary.admin_ledger_maintenance_interval_seconds
        ),
        admin_attempt_reconciliation_grace_seconds=(
            boundary.admin_attempt_reconciliation_grace_seconds
        ),
    )


def load_database_url(source: SettingsSource | None = None) -> SecretStr:
    """Build one async PostgreSQL DSN, optionally injecting a file-only password."""
    boundary = source if source is not None else SettingsSource()
    try:
        database_url = make_url(boundary.database_url.get_secret_value())
    except ArgumentError:
        raise ConfigurationError(code=ConfigurationErrorCode.DATABASE_URL_INVALID) from None
    if database_url.drivername != "postgresql+asyncpg" or database_url.database in {None, ""}:
        raise ConfigurationError(code=ConfigurationErrorCode.DATABASE_URL_INVALID)
    password_file = boundary.database_password_file
    if password_file is None:
        return SecretStr(database_url.render_as_string(hide_password=False))
    if database_url.password is not None:
        raise ConfigurationError(code=ConfigurationErrorCode.DATABASE_URL_INVALID)
    try:
        password_bytes = password_file.read_bytes()
    except OSError:
        raise ConfigurationError(code=ConfigurationErrorCode.DATABASE_URL_INVALID) from None
    if not 1 <= len(password_bytes) <= _DATABASE_PASSWORD_MAX_BYTES or any(
        byte in b"\x00\r\n" for byte in password_bytes
    ):
        raise ConfigurationError(code=ConfigurationErrorCode.DATABASE_URL_INVALID)
    try:
        password = password_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigurationError(code=ConfigurationErrorCode.DATABASE_URL_INVALID) from None
    with_password = database_url.set(password=password)
    return SecretStr(with_password.render_as_string(hide_password=False))
