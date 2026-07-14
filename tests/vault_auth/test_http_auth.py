import anyio
import pytest

from nvidia_build_lb.admin.schemas import (
    DownstreamScope,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
)
from nvidia_build_lb.schemas import ErrorEnvelope, ModelListResponse

from .conftest import ADMIN_TOKEN, CredentialHttpSlice

pytestmark = pytest.mark.vault_auth

_HOST = "127.0.0.1:2456"
_ORIGIN = "http://127.0.0.1:2456"


def _issue_token(
    http_slice: CredentialHttpSlice,
    label: str,
    scope: DownstreamScope,
) -> DownstreamTokenIssued:
    return anyio.run(
        http_slice.repositories.downstream.issue,
        DownstreamTokenIssueRequest(label=label, scopes=(scope,)),
        f"issue-{label}",
    )


def _headers(token: str) -> dict[str, str]:
    return {
        "Host": _HOST,
        "Origin": _ORIGIN,
        "Authorization": f"Bearer {token}",
    }


def test_http_wrong_admin_returns_exact_admin_realm(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: a shape-valid but wrong synthetic admin bearer.
    wrong = f"nblb_admin_{'f' * 64}"

    # When: it reaches an administration route.
    response = credential_http_slice.client.get(
        "/admin/api/v1/overview",
        headers=_headers(wrong),
    )

    # Then: the locked 401 realm is present and the candidate is not reflected.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="nvidia-build-lb-admin"'
    assert error.error.code == "admin_unauthorized"
    assert wrong not in response.text


def test_http_downstream_bearer_cannot_authenticate_as_admin(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one valid models-only downstream bearer.
    issued = _issue_token(
        credential_http_slice,
        "downstream-as-admin",
        DownstreamScope.MODELS_READ,
    )

    # When: it reaches an administration route.
    response = credential_http_slice.client.get(
        "/admin/api/v1/overview",
        headers=_headers(issued.token),
    )

    # Then: realm separation rejects it as an admin credential.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="nvidia-build-lb-admin"'
    assert error.error.code == "admin_unauthorized"


def test_http_admin_bearer_cannot_authenticate_downstream(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: the exact valid administration bearer.

    # When: it reaches the downstream models route.
    response = credential_http_slice.client.get(
        "/v1/models",
        headers=_headers(ADMIN_TOKEN),
    )

    # Then: it fails in the distinct downstream realm.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="nvidia-build-lb"'
    assert error.error.code == "unauthorized"


def test_http_models_scope_commits_counter_before_fixed_response(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one valid models-only downstream bearer.
    issued = _issue_token(credential_http_slice, "models-success", DownstreamScope.MODELS_READ)

    # When: it requests the local fixed model list.
    response = credential_http_slice.client.get(
        "/v1/models",
        headers=_headers(issued.token),
    )

    # Then: the exact response follows a durable authorized-use increment.
    models = ModelListResponse.model_validate_json(response.content)
    listed = anyio.run(credential_http_slice.repositories.downstream.list_all)
    assert response.status_code == 200
    assert models == ModelListResponse.fixed_model()
    assert listed.items[0].request_count == 1


def test_http_missing_scope_is_403_without_counter_increment(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one valid models-only downstream bearer.
    issued = _issue_token(credential_http_slice, "missing-chat", DownstreamScope.MODELS_READ)

    # When: it requests the chat-write-only route.
    response = credential_http_slice.client.post(
        "/v1/chat/completions",
        headers=_headers(issued.token),
        json={},
    )

    # Then: scope rejection is exact and no downstream use commits.
    error = ErrorEnvelope.model_validate_json(response.content)
    listed = anyio.run(credential_http_slice.repositories.downstream.list_all)
    assert response.status_code == 403
    assert error.error.code == "insufficient_scope"
    assert listed.items[0].request_count == 0


def test_http_revoked_token_is_401_without_counter_increment(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one downstream bearer revoked before first use.
    issued = _issue_token(credential_http_slice, "revoked-models", DownstreamScope.MODELS_READ)
    anyio.run(
        credential_http_slice.repositories.downstream.revoke,
        issued.id,
        "revoke-before-use",
    )

    # When: it requests the models route.
    response = credential_http_slice.client.get(
        "/v1/models",
        headers=_headers(issued.token),
    )

    # Then: the exact downstream 401 is returned with no authorized use.
    error = ErrorEnvelope.model_validate_json(response.content)
    listed = anyio.run(credential_http_slice.repositories.downstream.list_all)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="nvidia-build-lb"'
    assert error.error.code == "unauthorized"
    assert listed.items[0].request_count == 0


def test_http_valid_options_never_authenticates(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: a valid boundary with a malformed authorization value.

    # When: OPTIONS targets an administration route.
    response = credential_http_slice.client.options(
        "/admin/api/v1/overview",
        headers={"Host": _HOST, "Origin": _ORIGIN, "Authorization": "not-a-bearer"},
    )

    # Then: global 405 wins and no bearer realm is evaluated.
    assert response.status_code == 405
    assert "www-authenticate" not in response.headers


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        pytest.param(
            {"Host": "attacker.invalid", "Origin": "null", "Authorization": "not-a-bearer"},
            "host_forbidden",
            id="host-first",
        ),
        pytest.param(
            {"Host": _HOST, "Origin": "null", "Authorization": "not-a-bearer"},
            "origin_forbidden",
            id="origin-second",
        ),
    ],
)
def test_http_authority_rejection_precedes_authentication(
    credential_http_slice: CredentialHttpSlice,
    headers: dict[str, str],
    code: str,
) -> None:
    # Given: invalid authority headers plus a malformed credential.

    # When: the ordered request boundary receives them.
    response = credential_http_slice.client.get("/admin/api/v1/overview", headers=headers)

    # Then: Host or Origin wins without an authentication realm header.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 403
    assert error.error.code == code
    assert "www-authenticate" not in response.headers
