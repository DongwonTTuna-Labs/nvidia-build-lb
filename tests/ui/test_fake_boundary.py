from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Headers

from .fake_admin_server import create_fake_admin_app
from .fake_admin_state import FakeAdminState, SafeNetworkObservation

pytestmark = pytest.mark.ui_fake


@pytest.fixture
def state() -> FakeAdminState:
    return FakeAdminState()


@pytest.fixture
def client(state: FakeAdminState) -> Iterator[TestClient]:
    with TestClient(
        create_fake_admin_app(state),
        base_url="http://127.0.0.1:2456",
    ) as test_client:
        yield test_client


def _admin_headers(state: FakeAdminState) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {state.admin_bearer}",
        "Origin": "http://127.0.0.1:2456",
    }


@pytest.mark.parametrize(
    ("headers", "expected_code"),
    [
        ({"Host": "localhost:2456"}, "host_forbidden"),
        ({"Origin": "null"}, "origin_forbidden"),
        ({"Origin": "https://127.0.0.1:2456"}, "origin_forbidden"),
    ],
)
def test_boundary_rejects_host_or_origin_before_authentication(
    client: TestClient,
    headers: dict[str, str],
    expected_code: str,
) -> None:
    # Given: invalid boundary metadata and no bearer.
    # When: a protected route is requested.
    response = client.get("/admin/api/v1/overview", headers=headers)

    # Then: the boundary classification wins before authentication.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == expected_code


def test_options_stops_after_origin_without_authentication(
    client: TestClient,
    state: FakeAdminState,
) -> None:
    # Given: an origin-valid OPTIONS request without authorization.
    state.reset_boundary_audit()

    # When: it targets a protected route.
    response = client.options(
        "/admin/api/v1/overview",
        headers={"Origin": "http://127.0.0.1:2456"},
    )

    # Then: it is globally 405 and never reaches auth or route dispatch.
    assert response.status_code == 405
    assert state.boundary_audit() == ("host", "origin", "options_405")


def test_protected_route_records_exact_boundary_and_route_order(
    client: TestClient,
    state: FakeAdminState,
) -> None:
    # Given: exact authority, origin, and admin bearer.
    state.reset_boundary_audit()

    # When: the overview is requested.
    response = client.get("/admin/api/v1/overview", headers=_admin_headers(state))

    # Then: Host, Origin, auth, and route run in that order.
    assert response.status_code == 200
    assert state.boundary_audit() == ("host", "origin", "auth", "route:overview")


def test_unknown_admin_query_is_safe_validation_failure(
    client: TestClient,
    state: FakeAdminState,
) -> None:
    # Given: valid authentication and an unsupported query parameter.
    # When: the complete-list route is requested with a fake pagination input.
    response = client.get(
        "/admin/api/v1/upstream-keys?limit=1",
        headers=_admin_headers(state),
    )

    # Then: the fake matches the production 422 envelope without reflecting input.
    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "invalid_request",
            "message": "request validation failed",
            "request_id": "fake-request",
        }
    }
    assert "limit" not in response.text


def test_duplicate_host_fails_closed(client: TestClient) -> None:
    # Given: two Host field lines instead of the sole accepted authority.
    headers = Headers(
        [
            ("Host", "127.0.0.1:2456"),
            ("Host", "127.0.0.1:2456"),
        ]
    )

    # When: the shell is requested.
    response = client.get("/admin", headers=headers)

    # Then: duplicate authority is rejected without dispatch.
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "host_forbidden"


def test_fake_server_audit_projects_only_safe_network_fields(
    client: TestClient,
    state: FakeAdminState,
) -> None:
    # Given: an empty server-side network audit and a valid admin boundary.
    state.reset_network_audit()

    # When: the browser-equivalent overview request completes.
    response = client.get("/admin/api/v1/overview", headers=_admin_headers(state))

    # Then: method/path/status and a query-presence bit are the only retained fields.
    assert response.status_code == 200
    assert state.network_audit() == (
        SafeNetworkObservation(
            method="GET",
            path="/admin/api/v1/overview",
            status=200,
            query_present=False,
        ),
    )
    assert set(SafeNetworkObservation.__slots__) == {
        "method",
        "path",
        "status",
        "query_present",
    }


def test_data_fixture_reset_preserves_the_independent_network_audit(
    state: FakeAdminState,
) -> None:
    # Given: a response observation recorded before the next synthetic UI state.
    observation = SafeNetworkObservation(
        method="GET",
        path="/admin/api/v1/overview",
        status=401,
        query_present=False,
    )
    state.record_network(observation)

    # When: mutable fixture rows are restored for another state journey.
    state.reset()

    # Then: the phase-wide browser network evidence remains intact.
    assert state.network_audit() == (observation,)
