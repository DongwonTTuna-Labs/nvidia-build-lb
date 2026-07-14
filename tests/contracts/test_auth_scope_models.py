"""Authentication realms, scope, revocation, and model-list contracts."""

import pytest

from ._support import (
    ADMIN_TOKEN,
    CHAT_TOKEN,
    MODELS_TOKEN,
    REVOKED_TOKEN,
    ContractClient,
    assert_error,
    bearer,
)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "/v1/models", id="models"),
        pytest.param("POST", "/v1/chat/completions", id="chat"),
    ],
)
def test_admin_bearer_cannot_cross_into_downstream_realm(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(method, path, headers=bearer(ADMIN_TOKEN))

    assert_error(
        response,
        status_code=401,
        code="unauthorized",
        realm="nvidia-build-lb",
    )


def test_downstream_bearer_cannot_cross_into_admin_realm(
    contract_client: ContractClient,
) -> None:
    response = contract_client.request(
        "GET",
        "/admin/api/v1/overview",
        headers=bearer(MODELS_TOKEN),
    )

    assert_error(
        response,
        status_code=401,
        code="admin_unauthorized",
        realm="nvidia-build-lb-admin",
    )


@pytest.mark.parametrize(
    ("method", "path", "token"),
    [
        pytest.param("GET", "/v1/models", CHAT_TOKEN, id="models-read"),
        pytest.param("POST", "/v1/chat/completions", MODELS_TOKEN, id="chat-write"),
    ],
)
def test_valid_downstream_bearer_requires_exact_route_scope(
    contract_client: ContractClient,
    method: str,
    path: str,
    token: str,
) -> None:
    response = contract_client.request(method, path, headers=bearer(token))

    assert_error(response, status_code=403, code="insufficient_scope")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "/v1/models", id="models"),
        pytest.param("POST", "/v1/chat/completions", id="chat"),
    ],
)
def test_revoked_downstream_bearer_is_unauthorized(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(method, path, headers=bearer(REVOKED_TOKEN))

    assert_error(
        response,
        status_code=401,
        code="unauthorized",
        realm="nvidia-build-lb",
    )


def test_models_scope_returns_exact_fixed_model(contract_client: ContractClient) -> None:
    response = contract_client.request(
        "GET",
        "/v1/models",
        headers=bearer(MODELS_TOKEN),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == (
        b'{"object":"list","data":[{"id":"z-ai/glm-5.2","object":"model","owned_by":"nvidia"}]}'
    )
