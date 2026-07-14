"""Admin state route, response policy, and sensitive-field exclusion contracts."""

import pytest

from ._support import (
    ADMIN_TOKEN,
    DISABLED_KEY_ID,
    DOWNSTREAM_ID,
    ENABLED_KEY_ID,
    UPSTREAM_KEY,
    ContractClient,
    assert_error,
    assert_excludes,
    assert_security_headers,
    bearer,
)


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        pytest.param("GET", "/admin/api/v1/overview", 200, id="overview"),
        pytest.param("GET", "/admin/api/v1/upstream-keys", 200, id="upstream-list"),
        pytest.param(
            "POST",
            f"/admin/api/v1/upstream-keys/{DISABLED_KEY_ID}/enable",
            204,
            id="upstream-enable",
        ),
        pytest.param(
            "POST",
            f"/admin/api/v1/upstream-keys/{ENABLED_KEY_ID}/disable",
            204,
            id="upstream-disable",
        ),
        pytest.param(
            "POST",
            f"/admin/api/v1/upstream-keys/{ENABLED_KEY_ID}/probe",
            200,
            id="upstream-probe",
        ),
        pytest.param(
            "DELETE",
            f"/admin/api/v1/upstream-keys/{DISABLED_KEY_ID}",
            204,
            id="upstream-delete",
        ),
        pytest.param("GET", "/admin/api/v1/downstream-tokens", 200, id="downstream-list"),
        pytest.param(
            "DELETE",
            f"/admin/api/v1/downstream-tokens/{DOWNSTREAM_ID}",
            204,
            id="downstream-revoke",
        ),
        pytest.param("GET", "/admin/api/v1/events", 200, id="events"),
    ],
)
def test_seeded_admin_state_route_contract(
    contract_client: ContractClient,
    method: str,
    path: str,
    expected_status: int,
) -> None:
    response = contract_client.request(method, path, headers=bearer(ADMIN_TOKEN))

    assert response.status_code == expected_status
    if expected_status == 204:
        assert response.content == b""
    assert_security_headers(response)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/admin/api/v1/upstream-keys", id="upstream-list"),
        pytest.param("/admin/api/v1/downstream-tokens", id="downstream-list"),
        pytest.param("/admin/api/v1/events", id="events"),
    ],
)
def test_admin_read_models_exclude_sensitive_storage_and_wire_fields(
    contract_client: ContractClient,
    path: str,
) -> None:
    response = contract_client.request("GET", path, headers=bearer(ADMIN_TOKEN))

    assert response.status_code == 200
    assert_excludes(
        response,
        "plaintext",
        "digest",
        "nonce",
        "ciphertext",
        "headers",
        "message_body",
        "upstream_response_body",
        "filesystem_path",
    )


def test_wrong_admin_header_is_not_reflected(contract_client: ContractClient) -> None:
    wrong_header = f"Bearer nblb_admin_{'f' * 64}"
    response = contract_client.request(
        "GET",
        "/admin/api/v1/overview",
        headers={"Authorization": wrong_header},
    )

    assert_error(
        response,
        status_code=401,
        code="admin_unauthorized",
        realm="nvidia-build-lb-admin",
    )
    assert_excludes(response, wrong_header)


def test_upstream_plaintext_body_is_not_reflected(contract_client: ContractClient) -> None:
    response = contract_client.request(
        "POST",
        "/admin/api/v1/upstream-keys",
        headers=bearer(ADMIN_TOKEN),
        json_body={"key": UPSTREAM_KEY},
    )

    assert response.status_code == 201
    assert_excludes(response, UPSTREAM_KEY, "plaintext", "nonce", "ciphertext")


def test_sensitive_unknown_path_is_not_reflected(contract_client: ContractClient) -> None:
    path_marker = "sensitive-filesystem-path-marker"
    response = contract_client.request(
        "GET",
        f"/admin/api/v1/{path_marker}",
        headers=bearer(ADMIN_TOKEN),
    )

    assert_error(response, status_code=404, code="resource_not_found")
    assert_excludes(response, path_marker)


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        pytest.param("GET", "/admin", {}, id="admin-shell"),
        pytest.param("GET", "/assets/admin.css", {}, id="admin-asset"),
        pytest.param("GET", "/admin/api/v1/overview", bearer(ADMIN_TOKEN), id="admin-api"),
    ],
)
def test_browser_and_admin_surfaces_have_exact_security_policy(
    contract_client: ContractClient,
    method: str,
    path: str,
    headers: dict[str, str],
) -> None:
    response = contract_client.request(method, path, headers=headers)

    assert_security_headers(response)
