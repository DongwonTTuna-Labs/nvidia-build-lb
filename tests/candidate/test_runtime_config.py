"""Secret-file database configuration for the production container."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from nvidia_build_lb import prestart_validate
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


def _prestart_source(tmp_path: Path, ending: bytes) -> SettingsSource:
    password = tmp_path / "db_password"
    vault = tmp_path / "vault_master_key"
    admin = tmp_path / "admin_token"
    _ = password.write_bytes(b"synthetic-db-password")
    _ = vault.write_bytes(b"v" * 32)
    _ = admin.write_bytes(b"nblb_admin_" + (b"a" * 64) + ending)
    return SettingsSource(
        database_url=SecretStr("postgresql+asyncpg://nvidia_build_lb@db/nvidia_build_lb"),
        database_password_file=password,
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


@pytest.mark.parametrize("ending", [b"", b"\n"])
def test_app_prestart_matches_runtime_admin_token_newline_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: bytes,
) -> None:
    source = _prestart_source(tmp_path, ending)
    monkeypatch.setattr(prestart_validate, "_CANONICAL", tmp_path)

    assert prestart_validate.validate("app")
    _ = load_settings(source)


@pytest.mark.parametrize("ending", [b"\n\n", b"\r\n", b" "])
def test_prestart_and_runtime_reject_invalid_admin_token_suffixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: bytes,
) -> None:
    source = _prestart_source(tmp_path, ending)
    monkeypatch.setattr(prestart_validate, "_CANONICAL", tmp_path)

    assert not prestart_validate.validate("app")
    with pytest.raises(ConfigurationError) as captured:
        _ = load_settings(source)
    assert captured.value.code is ConfigurationErrorCode.ADMIN_TOKEN_INVALID
