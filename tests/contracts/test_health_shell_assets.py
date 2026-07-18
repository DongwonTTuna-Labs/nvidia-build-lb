"""Readiness, shell, exact asset, and method contracts."""

import pytest

from ._support import ContractClient, assert_security_headers


def test_health_ready_contract(contract_client: ContractClient) -> None:
    contract_client.set_ready(True)
    response = contract_client.request("GET", "/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == b'{"status":"ok","ready":true}'


def test_health_degraded_contract(contract_client: ContractClient) -> None:
    contract_client.set_ready(False)
    response = contract_client.request("GET", "/health")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert response.content == b'{"status":"degraded","ready":false}'


@pytest.mark.parametrize(
    ("path", "media_type"),
    [
        pytest.param("/admin", "text/html", id="admin-shell"),
        pytest.param("/showcase", "text/html", id="showcase-shell"),
        pytest.param("/assets/admin.css", "text/css", id="admin-css"),
        pytest.param("/assets/admin.js", "text/javascript", id="admin-js"),
        pytest.param("/assets/favicon.svg", "image/svg+xml", id="favicon"),
        pytest.param("/assets/showcase.css", "text/css", id="showcase-css"),
        pytest.param("/assets/showcase.js", "text/javascript", id="showcase-js"),
    ],
)
def test_exact_shell_and_asset_get_contract(
    contract_client: ContractClient,
    path: str,
    media_type: str,
) -> None:
    response = contract_client.request("GET", path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)
    assert response.content
    assert_security_headers(response)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("POST", "/health", id="health-post"),
        pytest.param("POST", "/v1/models", id="models-post"),
        pytest.param("GET", "/v1/chat/completions", id="chat-get"),
        pytest.param("POST", "/admin", id="admin-post"),
        pytest.param("POST", "/assets/admin.css", id="asset-post"),
    ],
)
def test_known_path_rejects_unlisted_method(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(method, path)

    assert response.status_code == 405


def test_unknown_asset_is_404_with_security_policy(contract_client: ContractClient) -> None:
    response = contract_client.request("GET", "/assets/unknown-contract.js")

    assert response.status_code == 404
    assert_security_headers(response)
