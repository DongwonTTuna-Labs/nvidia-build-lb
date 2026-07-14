"""Secret-file database configuration for the production container."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from nvidia_build_lb.config import SettingsSource, load_settings
from nvidia_build_lb.errors import ConfigurationError, ConfigurationErrorCode


def _source(tmp_path: Path, password_file: Path | None) -> SettingsSource:
    vault = tmp_path / "vault"
    admin = tmp_path / "admin"
    _ = vault.write_bytes(b"v" * 32)
    _ = admin.write_text(f"nblb_admin_{'a' * 64}", encoding="ascii")
    return SettingsSource(
        database_url=SecretStr("postgresql+asyncpg://nvidia_build_lb@db/nvidia_build_lb"),
        database_password_file=password_file,
        vault_key_file=vault,
        admin_token_file=admin,
    )


def test_database_password_file_is_injected_without_entering_safe_metadata(tmp_path: Path) -> None:
    password = tmp_path / "db-password"
    _ = password.write_text("qa-db-secret!%", encoding="utf-8")

    settings = load_settings(_source(tmp_path, password))

    database_url = settings.database_url.get_secret_value()
    assert "qa-db-secret" in database_url
    assert "%25" in database_url
    serialized = settings.safe_metadata().model_dump_json()
    assert "qa-db-secret" not in serialized
    assert str(tmp_path) not in serialized


@pytest.mark.parametrize("contents", [b"", b"line\nbreak", b"nul\x00byte", b"x" * 1025])
def test_database_password_file_rejects_invalid_bytes(tmp_path: Path, contents: bytes) -> None:
    password = tmp_path / "db-password"
    _ = password.write_bytes(contents)

    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(_source(tmp_path, password))

    assert captured.value.code is ConfigurationErrorCode.DATABASE_URL_INVALID
    assert str(tmp_path) not in str(captured.value)


def test_database_password_file_must_exist_without_leaking_its_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing-sensitive-name"

    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(_source(tmp_path, missing))

    assert captured.value.code is ConfigurationErrorCode.DATABASE_URL_INVALID
    assert str(missing) not in str(captured.value)
