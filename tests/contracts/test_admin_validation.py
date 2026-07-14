"""Safe administration validation and no-pagination contracts."""

import pytest
from httpx2 import Response
from pydantic import JsonValue

from nvidia_build_lb.admin.schemas import AdminValidationErrorResponse

from ._support import ADMIN_TOKEN, ContractClient, assert_excludes, bearer


def _assert_safe_validation_error(response: Response) -> None:
    assert response.status_code == 422
    assert response.headers.get("content-type") == "application/json"
    payload = AdminValidationErrorResponse.model_validate_json(response.content)
    assert payload.error.code == "invalid_request"
    assert payload.error.message == "request validation failed"
    assert payload.error.request_id
    assert_excludes(response, "detail", "input", "context", "header", "secret")


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"key": ""}, id="empty"),
        pytest.param({"key": "é" * 2049}, id="too-many-utf8-bytes"),
        pytest.param({"key": "opaque\x00value"}, id="nul"),
        pytest.param({"key": "opaque\rvalue"}, id="cr"),
        pytest.param({"key": "opaque\nvalue"}, id="lf"),
        pytest.param({"key": 7}, id="wrong-type"),
        pytest.param({"key": "opaque-value", "enabled": True}, id="extra-field"),
    ],
)
def test_upstream_create_validation_is_the_fixed_safe_422(
    contract_client: ContractClient,
    body: JsonValue,
) -> None:
    # Given: one invalid opaque-key request shape.

    # When: it reaches the authenticated admin boundary.
    response = contract_client.request(
        "POST",
        "/admin/api/v1/upstream-keys",
        headers=bearer(ADMIN_TOKEN),
        json_body=body,
    )

    # Then: rejected input and framework detail are replaced by one safe envelope.
    _assert_safe_validation_error(response)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"label": "", "scopes": ["models:read"]}, id="blank-label"),
        pytest.param({"label": " leading", "scopes": ["models:read"]}, id="leading-space"),
        pytest.param({"label": "operator", "scopes": []}, id="empty-scopes"),
        pytest.param(
            {"label": "operator", "scopes": ["models:read", "models:read"]},
            id="duplicate-scopes",
        ),
        pytest.param({"label": "operator", "scopes": ["models:write"]}, id="unknown-scope"),
        pytest.param(
            {"label": "operator", "scopes": ["models:read"], "token": "forbidden"},
            id="extra-field",
        ),
    ],
)
def test_downstream_issue_validation_is_the_fixed_safe_422(
    contract_client: ContractClient,
    body: JsonValue,
) -> None:
    # Given: one invalid label, scope, or extra-field request shape.

    # When: it reaches the authenticated admin boundary.
    response = contract_client.request(
        "POST",
        "/admin/api/v1/downstream-tokens",
        headers=bearer(ADMIN_TOKEN),
        json_body=body,
    )

    # Then: rejected input and framework detail are replaced by one safe envelope.
    _assert_safe_validation_error(response)


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/admin/api/v1/upstream-keys?limit=1", id="upstream"),
        pytest.param("/admin/api/v1/downstream-tokens?cursor=x", id="downstream"),
        pytest.param("/admin/api/v1/overview?filter=x", id="overview"),
        pytest.param("/admin/api/v1/events?limit=1", id="events"),
    ],
)
def test_admin_reads_reject_every_pagination_or_filter_query(
    contract_client: ContractClient,
    path: str,
) -> None:
    # Given: a fixed admin read route carrying an unsupported query.

    # When: it reaches the authenticated administration boundary.
    response = contract_client.request("GET", path, headers=bearer(ADMIN_TOKEN))

    # Then: the route fails closed through the same safe validation envelope.
    _assert_safe_validation_error(response)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        pytest.param(
            "POST",
            "/admin/api/v1/upstream-keys/AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA/enable",
            id="uppercase",
        ),
        pytest.param(
            "POST",
            "/admin/api/v1/upstream-keys/aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa/disable",
            id="unhyphenated",
        ),
        pytest.param(
            "POST",
            "/admin/api/v1/upstream-keys/%7Baaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa%7D/probe",
            id="braced",
        ),
        pytest.param(
            "DELETE",
            "/admin/api/v1/downstream-tokens/not-a-uuid",
            id="invalid",
        ),
    ],
)
def test_admin_path_id_requires_canonical_lowercase_uuid(
    contract_client: ContractClient,
    method: str,
    path: str,
) -> None:
    response = contract_client.request(method, path, headers=bearer(ADMIN_TOKEN))

    _assert_safe_validation_error(response)
