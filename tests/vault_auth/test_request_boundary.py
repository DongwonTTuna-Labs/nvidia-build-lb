from collections.abc import Generator
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from nvidia_build_lb.request_boundary import RequestBoundaryMiddleware

pytestmark = pytest.mark.vault_auth

_HOST = "127.0.0.1:2456"
_ORIGIN = "http://127.0.0.1:2456"


class _ErrorDetail(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str


class _ErrorEnvelope(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    error: _ErrorDetail


@pytest.fixture
def boundary_client() -> Generator[tuple[TestClient, list[str]]]:
    dispatched: list[str] = []
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
    async def observed(path: str) -> dict[str, str]:
        dispatched.append(path)
        return {"path": path}

    _ = observed
    app.add_middleware(RequestBoundaryMiddleware)
    with TestClient(app, base_url=f"http://{_HOST}") as client:
        yield client, dispatched


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        pytest.param({"Host": "attacker.invalid", "Origin": _ORIGIN}, "host_forbidden", id="host"),
        pytest.param(
            {"Host": f"{_HOST},attacker", "Origin": _ORIGIN}, "host_forbidden", id="comma-host"
        ),
        pytest.param({"Host": _HOST, "Origin": "null"}, "origin_forbidden", id="null-origin"),
        pytest.param(
            {"Host": _HOST, "Origin": "https://127.0.0.1:2456"},
            "origin_forbidden",
            id="https-origin",
        ),
    ],
)
def test_invalid_host_or_origin_prevents_route_dispatch(
    boundary_client: tuple[TestClient, list[str]],
    headers: dict[str, str],
    code: str,
) -> None:
    # Given: a route-observing app and one invalid raw boundary shape.
    client, dispatched = boundary_client

    # When: the request reaches Host and Origin validation.
    response = client.get("/observed", headers=headers)

    # Then: it fails safely before the inner app and grants no CORS access.
    payload = _ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 403
    assert payload.error.code == code
    assert dispatched == []
    assert not any(name.lower().startswith(b"access-control-") for name, _ in response.headers.raw)
    assert "attacker" not in response.text


def test_valid_options_is_global_405_without_dispatch_or_auth(
    boundary_client: tuple[TestClient, list[str]],
) -> None:
    # Given: a route that would otherwise accept OPTIONS.
    client, dispatched = boundary_client

    # When: a valid origin-less OPTIONS request reaches the boundary.
    response = client.options("/observed", headers={"Host": _HOST})

    # Then: global 405 terminates before route dispatch.
    assert response.status_code == 405
    assert dispatched == []
    assert "access-control-allow-origin" not in response.headers


def test_invalid_options_still_fails_host_before_global_405(
    boundary_client: tuple[TestClient, list[str]],
) -> None:
    # Given: an OPTIONS request with both an invalid Host and Origin.
    client, dispatched = boundary_client

    # When: it reaches the ordered boundary.
    response = client.options(
        "/observed",
        headers={"Host": "attacker.invalid", "Origin": "null"},
    )

    # Then: Host rejection wins and route dispatch remains untouched.
    payload = _ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 403
    assert payload.error.code == "host_forbidden"
    assert dispatched == []


def test_valid_non_options_dispatches_and_admin_path_gets_security_policy(
    boundary_client: tuple[TestClient, list[str]],
) -> None:
    # Given: one accepted Host and same Origin.
    client, dispatched = boundary_client

    # When: an admin-prefixed non-OPTIONS route is requested.
    response = client.get(
        "/admin/api/v1/overview",
        headers={"Host": _HOST, "Origin": _ORIGIN},
    )

    # Then: it dispatches once and receives the exact no-store browser policy.
    assert response.status_code == 200
    assert dispatched == ["admin/api/v1/overview"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]


def test_showcase_shell_gets_the_same_security_policy(
    boundary_client: tuple[TestClient, list[str]],
) -> None:
    # Given: the exact public showcase shell path.
    client, dispatched = boundary_client

    # When: the accepted origin-less request reaches the inner route.
    response = client.get("/showcase", headers={"Host": _HOST})

    # Then: the shell is dispatched and receives the locked browser policy.
    assert response.status_code == 200
    assert dispatched == ["showcase"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize(
    ("headers", "code"),
    [
        pytest.param(
            [("Host", _HOST), ("Host", _HOST), ("Origin", _ORIGIN)],
            "host_forbidden",
            id="duplicate-host",
        ),
        pytest.param(
            [("Host", _HOST), ("Origin", _ORIGIN), ("Origin", _ORIGIN)],
            "origin_forbidden",
            id="duplicate-origin",
        ),
        pytest.param(
            [("Host", _HOST), ("Origin", f"{_ORIGIN},{_ORIGIN}")],
            "origin_forbidden",
            id="comma-origin",
        ),
    ],
)
def test_duplicate_or_joined_authority_headers_fail_closed(
    boundary_client: tuple[TestClient, list[str]],
    headers: list[tuple[str, str]],
    code: str,
) -> None:
    # Given: duplicate or comma-joined authority headers.
    client, dispatched = boundary_client

    # When: the raw header list reaches the ordered boundary.
    response = client.get("/observed", headers=headers)

    # Then: the boundary rejects it before dispatch with the exact class.
    payload = _ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 403
    assert payload.error.code == code
    assert dispatched == []
