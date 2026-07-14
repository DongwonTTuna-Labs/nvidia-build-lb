"""Typed errors and their non-sensitive serialization."""

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never, override


class ConfigurationErrorCode(StrEnum):
    """Stable settings-boundary failure codes."""

    DATABASE_URL_INVALID = "config_database_url_invalid"
    LOG_LEVEL_INVALID = "config_log_level_invalid"
    STAGE_INVALID = "config_stage_invalid"
    VAULT_KEY_UNAVAILABLE = "config_vault_key_unavailable"
    VAULT_KEY_INVALID = "config_vault_key_invalid"
    ADMIN_CREDENTIAL_UNAVAILABLE = "config_admin_token_unavailable"
    ADMIN_CREDENTIAL_INVALID = "config_admin_token_invalid"
    ADMIN_TOKEN_UNAVAILABLE = ADMIN_CREDENTIAL_UNAVAILABLE
    ADMIN_TOKEN_INVALID = ADMIN_CREDENTIAL_INVALID


@dataclass(slots=True)
class ConfigurationError(Exception):
    """A fail-closed configuration error that retains no sensitive context."""

    code: ConfigurationErrorCode

    @property
    def public_message(self) -> str:
        """Return the stable operator-safe message for this error."""
        match self.code:
            case ConfigurationErrorCode.DATABASE_URL_INVALID:
                message = "database URL must be a PostgreSQL async DSN"
            case ConfigurationErrorCode.LOG_LEVEL_INVALID:
                message = "log level is invalid"
            case ConfigurationErrorCode.STAGE_INVALID:
                message = "deployment stage is invalid"
            case ConfigurationErrorCode.VAULT_KEY_UNAVAILABLE:
                message = "vault key is unavailable"
            case ConfigurationErrorCode.VAULT_KEY_INVALID:
                message = "vault key must contain exactly 32 bytes"
            case ConfigurationErrorCode.ADMIN_CREDENTIAL_UNAVAILABLE:
                message = "admin token is unavailable"
            case ConfigurationErrorCode.ADMIN_CREDENTIAL_INVALID:
                message = "admin token format is invalid"
            case _:
                assert_never(self.code)
        return message

    @override
    def __str__(self) -> str:
        """Return only the stable code, never the rejected value or path."""
        return self.code.value


@dataclass(frozen=True, slots=True)
class SafeError:
    """Whitelisted error fields allowed in responses and logs."""

    error_type: str
    code: ConfigurationErrorCode
    message: str


def safe_error(error: ConfigurationError) -> SafeError:
    """Reduce a typed error to its explicitly whitelisted public fields."""
    return SafeError(
        error_type=type(error).__name__,
        code=error.code,
        message=error.public_message,
    )
