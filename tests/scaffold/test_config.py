from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from nvidia_build_lb.config import (
    NVIDIA_BASE_URL,
    NVIDIA_MODEL,
    DeploymentStage,
    LogLevel,
    SettingsSource,
    load_settings,
)
from nvidia_build_lb.errors import ConfigurationError, ConfigurationErrorCode


def _valid_source(tmp_path: Path) -> SettingsSource:
    vault_path = tmp_path / "vault.key"
    admin_path = tmp_path / "admin.token"
    _ = vault_path.write_bytes(bytes(range(32)))
    _ = admin_path.write_text(f"nblb_admin_{'a' * 64}\n", encoding="ascii")
    vault_path.chmod(0o600)
    admin_path.chmod(0o600)
    return SettingsSource(
        database_url=SecretStr("postgresql+asyncpg://nvidia_build_lb@127.0.0.1/nvidia_build_lb"),
        vault_key_file=vault_path,
        admin_token_file=admin_path,
        stage="production",
        log_level="INFO",
    )


def test_settings_return_only_safe_metadata_when_secret_files_are_valid(
    tmp_path: Path,
) -> None:
    # Given: valid synthetic secret files and non-secret database configuration.
    source = _valid_source(tmp_path)

    # When: the settings boundary loads and parses them.
    metadata = load_settings(source).safe_metadata()

    # Then: only stable non-secret metadata is serializable.
    assert metadata.base_url == NVIDIA_BASE_URL
    assert metadata.model == NVIDIA_MODEL
    assert metadata.stage is DeploymentStage.PRODUCTION
    assert metadata.log_level is LogLevel.INFO
    assert metadata.secret_files_loaded is True
    serialized = metadata.model_dump_json()
    assert "nblb_admin_" not in serialized
    assert str(tmp_path) not in serialized


def test_settings_source_accepts_compose_decimal_environment_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "ADMIN_EVENT_MAX_ROWS": 200_000,
        "ADMIN_ATTEMPT_MAX_ROWS": 80_000,
        "ADMIN_LEDGER_PRUNE_BATCH_SIZE": 2_000,
    }
    for name, value in values.items():
        monkeypatch.setenv(f"NVIDIA_BUILD_LB_{name}", str(value))

    source = SettingsSource()

    assert source.admin_event_max_rows == values["ADMIN_EVENT_MAX_ROWS"]
    assert source.admin_attempt_max_rows == values["ADMIN_ATTEMPT_MAX_ROWS"]
    assert source.admin_ledger_prune_batch_size == values["ADMIN_LEDGER_PRUNE_BATCH_SIZE"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ADMIN_EVENT_MAX_ROWS", "01000"),
        ("ADMIN_ATTEMPT_MAX_ROWS", "0100"),
        ("ADMIN_LEDGER_PRUNE_BATCH_SIZE", "06"),
        ("ADMIN_EVENT_MAX_ROWS", "+1000"),
        ("ADMIN_EVENT_MAX_ROWS", " 1000"),
        ("ADMIN_EVENT_MAX_ROWS", "\uff11\uff10\uff10\uff10"),
    ],
)
def test_settings_source_rejects_noncanonical_compose_integers(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(f"NVIDIA_BUILD_LB_{name}", value)

    with pytest.raises(ValidationError):
        _ = SettingsSource()


def test_settings_reject_a_missing_vault_file_without_its_path(tmp_path: Path) -> None:
    # Given: a valid source whose vault path does not exist.
    source = _valid_source(tmp_path).model_copy(
        update={"vault_key_file": tmp_path / "sensitive-vault-location"}
    )

    # When: the settings boundary tries to load it.
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)

    # Then: the typed rejection contains only its stable code.
    assert captured.value.code is ConfigurationErrorCode.VAULT_KEY_UNAVAILABLE
    assert str(tmp_path) not in str(captured.value)


def test_settings_reject_a_short_vault_key(tmp_path: Path) -> None:
    # Given: a vault file that is one byte shorter than AES-256 requires.
    source = _valid_source(tmp_path)
    _ = source.vault_key_file.write_bytes(bytes(range(31)))

    # When: the settings boundary parses the secret.
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)

    # Then: it returns the stable malformed-key classification.
    assert captured.value.code is ConfigurationErrorCode.VAULT_KEY_INVALID


def test_settings_reject_a_missing_admin_token_file(tmp_path: Path) -> None:
    # Given: a valid source whose admin-token path does not exist.
    source = _valid_source(tmp_path).model_copy(
        update={"admin_token_file": tmp_path / "sensitive-admin-location"}
    )

    # When: the settings boundary tries to load it.
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)

    # Then: the typed rejection contains no sensitive path.
    assert captured.value.code is ConfigurationErrorCode.ADMIN_TOKEN_UNAVAILABLE
    assert str(tmp_path) not in str(captured.value)


def test_settings_reject_a_malformed_admin_token(tmp_path: Path) -> None:
    # Given: an admin token without 256 bits of lowercase hexadecimal entropy.
    source = _valid_source(tmp_path)
    _ = source.admin_token_file.write_text("nblb_admin_too-short\n", encoding="ascii")

    # When: the settings boundary parses the secret.
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)

    # Then: it returns the stable malformed-token classification.
    assert captured.value.code is ConfigurationErrorCode.ADMIN_TOKEN_INVALID


def test_settings_reject_an_invalid_database_url(tmp_path: Path) -> None:
    # Given: a source with a value that is not a PostgreSQL DSN.
    source = _valid_source(tmp_path).model_copy(
        update={"database_url": SecretStr("invalid-database-url")}
    )

    # When: the settings boundary parses the DSN.
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)

    # Then: the value is reduced to a stable non-secret error code.
    assert captured.value.code is ConfigurationErrorCode.DATABASE_URL_INVALID
    assert "invalid-database-url" not in str(captured.value)


def test_settings_reject_a_database_url_without_a_database_name(tmp_path: Path) -> None:
    # Given: a parseable PostgreSQL URL that does not select a database.
    source = _valid_source(tmp_path).model_copy(
        update={"database_url": SecretStr("postgresql+asyncpg://nvidia_build_lb@127.0.0.1")}
    )

    # When: the settings boundary parses the incomplete DSN.
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)

    # Then: it fails closed with the same safe DSN classification.
    assert captured.value.code is ConfigurationErrorCode.DATABASE_URL_INVALID


def test_settings_reject_an_invalid_log_level(tmp_path: Path) -> None:
    # Given: a source with an unsupported log-level string.
    source = _valid_source(tmp_path).model_copy(update={"log_level": "TRACE"})

    # When: the settings boundary parses the level.
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)

    # Then: it returns the stable invalid-level classification.
    assert captured.value.code is ConfigurationErrorCode.LOG_LEVEL_INVALID


def test_prune_batch_must_fit_one_complete_two_attempt_group(tmp_path: Path) -> None:
    source = _valid_source(tmp_path)

    accepted = load_settings(source.model_copy(update={"admin_ledger_prune_batch_size": 6}))
    assert accepted.admin_ledger_prune_batch_size == 6

    rejected = source.model_copy(update={"admin_ledger_prune_batch_size": 5})
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(rejected)
    assert captured.value.code is ConfigurationErrorCode.ADMIN_LEDGER_INVALID


def test_attempt_capacity_preserves_newest_event_reserve(tmp_path: Path) -> None:
    source = _valid_source(tmp_path).model_copy(
        update={
            "admin_attempt_max_rows": 500,
            "admin_event_max_rows": 1_099,
        }
    )

    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)
    assert captured.value.code is ConfigurationErrorCode.ADMIN_LEDGER_INVALID
