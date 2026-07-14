from importlib import import_module

import pytest

pytestmark = pytest.mark.vault_auth

_SURFACES = {
    "credential_types": {
        "AuthenticationRejectedError",
        "AuthRealm",
        "Clock",
        "DownstreamPrincipal",
        "InsufficientScopeError",
        "ResourceConflictError",
        "ResourceNotFoundError",
    },
    "upstream_keys": {"UpstreamKeyDependencies", "UpstreamKeyRepository"},
    "downstream_tokens": {
        "DownstreamTokenDependencies",
        "DownstreamTokenRepository",
        "TokenHexSource",
    },
    "scheduler_state": {
        "AttemptLease",
        "AttemptTerminal",
        "SchedulerDependencies",
        "SchedulerStateRepository",
        "TerminalOutcome",
    },
    "auth": {"AdminAuthenticator", "DownstreamAuthenticator"},
    "request_boundary": {"RequestBoundaryMiddleware"},
    "admin_credentials": {
        "CredentialAuthenticators",
        "CredentialRepositories",
        "CredentialServices",
        "create_credential_test_app",
    },
}


@pytest.mark.parametrize(("module_name", "symbols"), _SURFACES.items())
def test_todo2_module_exports_its_closed_surface(
    module_name: str,
    symbols: set[str],
) -> None:
    # Given: an importable Todo 2 domain module.
    module = import_module(f"nvidia_build_lb.{module_name}")

    # When: its contract-owned symbol set is inspected.
    available = {name for name in symbols if hasattr(module, name)}

    # Then: every closed boundary type is exported.
    assert available == symbols
