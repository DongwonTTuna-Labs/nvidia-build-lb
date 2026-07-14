"""Value-silent validation for fixed canonical prestart secret files."""

import sys
from pathlib import Path

_CANONICAL = Path("/run/canonical-secrets")
_DATABASE_PASSWORD_MAX_BYTES = 1024
_ADMIN_TOKEN_HEX_BYTES = 64
_VAULT_KEY_BYTES = 32
_EXPECTED_ARGUMENT_COUNT = 2


def _valid_database_password() -> bool:
    value = (_CANONICAL / "db_password").read_bytes()
    if not 1 <= len(value) <= _DATABASE_PASSWORD_MAX_BYTES or any(
        byte in b"\x00\r\n" for byte in value
    ):
        return False
    try:
        _ = value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _valid_admin_token() -> bool:
    value = (_CANONICAL / "admin_token").read_bytes()
    prefix = b"nblb_admin_"
    suffix = value.removeprefix(prefix)
    return (
        value.startswith(prefix)
        and len(suffix) == _ADMIN_TOKEN_HEX_BYTES
        and all(byte in b"0123456789abcdef" for byte in suffix)
    )


def validate(mode: str) -> bool:
    """Validate only the fixed secret set required by one process mode."""
    try:
        if not _valid_database_password():
            return False
        if mode == "migrate":
            return True
        return (
            mode == "app"
            and _valid_admin_token()
            and len((_CANONICAL / "vault_master_key").read_bytes()) == _VAULT_KEY_BYTES
        )
    except OSError:
        return False


def main() -> None:
    """Exit without writing rejected values or paths."""
    mode = sys.argv[1] if len(sys.argv) == _EXPECTED_ARGUMENT_COUNT else ""
    raise SystemExit(0 if validate(mode) else 1)


if __name__ == "__main__":
    main()
