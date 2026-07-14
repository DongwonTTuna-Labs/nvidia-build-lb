from importlib import import_module
from importlib.util import find_spec

import pytest

pytestmark = pytest.mark.vault_auth

_TODO2_MODULES = (
    "credential_types",
    "upstream_keys",
    "downstream_tokens",
    "scheduler_state",
    "auth",
    "request_boundary",
    "admin_credentials",
)


@pytest.mark.parametrize("module_name", _TODO2_MODULES)
def test_todo2_module_is_packaged(module_name: str) -> None:
    # Given: one contract-owned Todo 2 module name.

    # When: the installed package resolves it.
    spec = find_spec(f"nvidia_build_lb.{module_name}")

    # Then: the module is importable without composition-root side effects.
    assert spec is not None


def test_database_module_exports_transaction_scope() -> None:
    # Given: the existing lazy database construction module.
    module = import_module("nvidia_build_lb.db")

    # When: its Todo 2 transaction symbol is inspected.
    available = hasattr(module, "transaction_scope")

    # Then: repositories can request an explicitly committed transaction.
    assert available is True
