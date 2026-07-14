"""Host, Origin, authentication, route, and global OPTIONS ordering."""

import pytest

from ._support import (
    ACCEPTED_HOST,
    ACCEPTED_ORIGIN,
    ADMIN_TOKEN,
    ContractClient,
    assert_error,
    assert_no_cors,
    bearer,
)

_ORDERED_CASES = [
    pytest.param("GET", "/health", id="health-noop"),
    pytest.param("GET", "/v1/models", id="models-auth"),
    pytest.param("POST", "/v1/chat/completions", id="chat-auth"),
    pytest.param("GET", "/admin", id="admin-noop"),
    pytest.param("GET", "/admin/api/v1/overview", id="admin-auth"),
    pytest.param("GET", "/not-a-route", id="unknown-route"),
]


@pytest.mark.parametrize(("method", "path"), _ORDERED_CASES)
def test_host_rejection_precedes_origin_auth_and_route(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(
        method,
        path,
        headers={"Host": "attacker.invalid", "Origin": "https://attacker.invalid"},
    )

    assert_error(response, status_code=403, code="host_forbidden")


@pytest.mark.parametrize(("method", "path"), _ORDERED_CASES)
def test_origin_rejection_precedes_auth_and_route(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(
        method,
        path,
        headers={"Host": ACCEPTED_HOST, "Origin": "https://attacker.invalid"},
    )

    assert_error(response, status_code=403, code="origin_forbidden")


_ADMIN_ROUTES = [
    pytest.param("GET", "/admin/api/v1/overview", id="overview"),
    pytest.param("GET", "/admin/api/v1/upstream-keys", id="upstream-list"),
    pytest.param("POST", "/admin/api/v1/upstream-keys", id="upstream-create"),
    pytest.param(
        "POST",
        "/admin/api/v1/upstream-keys/key-id/enable",
        id="upstream-enable",
    ),
    pytest.param(
        "POST",
        "/admin/api/v1/upstream-keys/key-id/disable",
        id="upstream-disable",
    ),
    pytest.param(
        "POST",
        "/admin/api/v1/upstream-keys/key-id/probe",
        id="upstream-probe",
    ),
    pytest.param(
        "DELETE",
        "/admin/api/v1/upstream-keys/key-id",
        id="upstream-delete",
    ),
    pytest.param("GET", "/admin/api/v1/downstream-tokens", id="downstream-list"),
    pytest.param("POST", "/admin/api/v1/downstream-tokens", id="downstream-issue"),
    pytest.param(
        "DELETE",
        "/admin/api/v1/downstream-tokens/token-id",
        id="downstream-revoke",
    ),
    pytest.param("GET", "/admin/api/v1/events", id="events"),
]


@pytest.mark.parametrize(("method", "path"), _ADMIN_ROUTES)
def test_exact_admin_route_auth_contract(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(method, path)

    assert_error(
        response,
        status_code=401,
        code="admin_unauthorized",
        realm="nvidia-build-lb-admin",
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param("GET", "/v1/models", id="models"),
        pytest.param("POST", "/v1/chat/completions", id="chat"),
    ],
)
def test_public_route_auth_contract(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(method, path)

    assert_error(
        response,
        status_code=401,
        code="unauthorized",
        realm="nvidia-build-lb",
    )


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/health", id="health"),
        pytest.param("/v1/models", id="models"),
        pytest.param("/v1/chat/completions", id="chat"),
        pytest.param("/admin", id="admin-shell"),
        pytest.param("/admin/api/v1/overview", id="admin-api"),
        pytest.param("/assets/admin.css", id="asset"),
        pytest.param("/not-a-route", id="unknown"),
    ],
)
def test_valid_options_is_global_405(contract_client: ContractClient, path: str) -> None:
    response = contract_client.request("OPTIONS", path)

    assert response.status_code == 405
    assert_no_cors(response)


def test_options_invalid_host_is_403(contract_client: ContractClient) -> None:
    response = contract_client.request(
        "OPTIONS",
        "/health",
        headers={"Host": "attacker.invalid", "Origin": ACCEPTED_ORIGIN},
    )

    assert_error(response, status_code=403, code="host_forbidden")


def test_options_invalid_origin_is_403(contract_client: ContractClient) -> None:
    response = contract_client.request(
        "OPTIONS",
        "/health",
        headers={"Host": ACCEPTED_HOST, "Origin": "null"},
    )

    assert_error(response, status_code=403, code="origin_forbidden")


@pytest.mark.parametrize(
    "path",
    [
        "/admin/api/v1",
        "/admin/api/v1/",
        "/admin/api/v1/overview/",
        "/admin/api/v1/unknown-resource",
    ],
)
def test_unknown_admin_namespace_authenticates_before_safe_404(
    contract_client: ContractClient,
    path: str,
) -> None:
    request_headers = {"Host": ACCEPTED_HOST, "Origin": ACCEPTED_ORIGIN}
    unauthenticated = contract_client.http.request(
        "GET",
        path,
        headers=request_headers,
        follow_redirects=False,
    )
    authenticated = contract_client.http.request(
        "GET",
        path,
        headers={**request_headers, **bearer(ADMIN_TOKEN)},
        follow_redirects=False,
    )

    assert_error(
        unauthenticated,
        status_code=401,
        code="admin_unauthorized",
        realm="nvidia-build-lb-admin",
    )
    assert_error(authenticated, status_code=404, code="resource_not_found")
    assert "location" not in authenticated.headers


def test_known_admin_resource_wrong_method_is_unauthenticated_405(
    contract_client: ContractClient,
) -> None:
    response = contract_client.request("POST", "/admin/api/v1/overview")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert "www-authenticate" not in response.headers
