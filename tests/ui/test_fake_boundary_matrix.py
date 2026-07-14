from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

import anyio
import pytest
from fastapi.testclient import TestClient
from httpx import Headers
from httpx2 import Response
from pydantic import TypeAdapter
from starlette.types import ASGIApp, Message, Scope

from .fake_admin_server import create_fake_admin_app
from .fake_admin_state import BoundaryStep, FakeAdminState

pytestmark = pytest.mark.ui_fake

_STATUS = TypeAdapter(int)
_BODY = TypeAdapter(bytes)
_KEEP_HOST: Final = False


@dataclass(frozen=True, slots=True)
class _BoundaryCase:
    headers: tuple[tuple[str, str], ...]
    remove_host: bool
    expected_status: int
    expected_code: str | None
    expected_steps: tuple[BoundaryStep, ...]


@dataclass(frozen=True, slots=True)
class _ResponseCase:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]


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


def _admin_headers(state: FakeAdminState) -> tuple[tuple[str, str], ...]:
    return (
        ("Authorization", f"Bearer {state.admin_bearer}"),
        ("Origin", "http://127.0.0.1:2456"),
    )


def _send(client: TestClient, case: _BoundaryCase) -> Response:
    request = client.build_request(
        "GET",
        "/admin/api/v1/overview",
        headers=Headers(case.headers),
    )
    if case.remove_host:
        del request.headers["host"]
    return client.send(request)


async def _request_without_host(application: ASGIApp) -> tuple[int, bytes]:
    messages: list[Message] = []
    request_pending = True

    async def receive() -> Message:
        nonlocal request_pending
        if request_pending:
            request_pending = False
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/admin/api/v1/overview",
        "raw_path": b"/admin/api/v1/overview",
        "query_string": b"",
        "root_path": "",
        "headers": (),
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 2456),
    }
    await application(scope, receive, send)
    start = messages[0]
    body = messages[-1]
    assert start["type"] == "http.response.start"
    assert body["type"] == "http.response.body"
    return _STATUS.validate_python(start["status"]), _BODY.validate_python(body["body"])


@pytest.mark.parametrize(
    "case",
    [
        _BoundaryCase(
            (("Host", "127.0.0.1:2456"), ("Host", "127.0.0.1:2456")),
            _KEEP_HOST,
            403,
            "host_forbidden",
            ("host",),
        ),
        _BoundaryCase(
            (("Host", "127.0.0.1:2456,127.0.0.1:2456"),),
            _KEEP_HOST,
            403,
            "host_forbidden",
            ("host",),
        ),
        _BoundaryCase(
            (("Host", "localhost:2456"),),
            _KEEP_HOST,
            403,
            "host_forbidden",
            ("host",),
        ),
        _BoundaryCase(
            (
                ("Origin", "http://127.0.0.1:2456"),
                ("Origin", "http://127.0.0.1:2456"),
            ),
            _KEEP_HOST,
            403,
            "origin_forbidden",
            ("host", "origin"),
        ),
        _BoundaryCase(
            (("Origin", "http://127.0.0.1:2456,http://127.0.0.1:2456"),),
            _KEEP_HOST,
            403,
            "origin_forbidden",
            ("host", "origin"),
        ),
        _BoundaryCase(
            (("Origin", "null"),),
            _KEEP_HOST,
            403,
            "origin_forbidden",
            ("host", "origin"),
        ),
        _BoundaryCase(
            (("Origin", "https://127.0.0.1:2456"),),
            _KEEP_HOST,
            403,
            "origin_forbidden",
            ("host", "origin"),
        ),
    ],
)
def test_host_and_origin_malformed_field_matrix_fails_in_exact_order(
    client: TestClient,
    state: FakeAdminState,
    case: _BoundaryCase,
) -> None:
    # Given: one missing, duplicate, comma-joined, or different boundary field shape.
    state.reset_boundary_audit()

    # When: the protected route crosses the fake browser boundary.
    response = _send(client, case)

    # Then: the exact boundary rejects before auth or route dispatch.
    assert response.status_code == case.expected_status
    assert response.json()["error"]["code"] == case.expected_code
    assert state.boundary_audit() == case.expected_steps


def test_missing_host_fails_before_origin_auth_and_route(state: FakeAdminState) -> None:
    # Given: a raw ASGI request with no Host field at all.
    application = create_fake_admin_app(state)
    state.reset_boundary_audit()

    # When: the request crosses the exact fake boundary.
    status, body = anyio.run(_request_without_host, application)

    # Then: Host rejects first with the safe envelope.
    assert status == 403
    assert b'"code":"host_forbidden"' in body
    assert state.boundary_audit() == ("host",)


def test_missing_origin_is_the_explicit_allowed_cli_shape(
    client: TestClient,
    state: FakeAdminState,
) -> None:
    # Given: exact Host and authentication with no Origin field.
    state.reset_boundary_audit()
    headers = (("Authorization", f"Bearer {state.admin_bearer}"),)

    # When: an origin-less CLI-equivalent request reaches Overview.
    response = client.get("/admin/api/v1/overview", headers=Headers(headers))

    # Then: Host, Origin no-op, auth, and route all complete.
    assert response.status_code == 200
    assert state.boundary_audit() == ("host", "origin", "auth", "route:overview")


@pytest.mark.parametrize(
    "case",
    [
        _ResponseCase("GET", "/admin", ()),
        _ResponseCase("GET", "/admin/api/v1/overview", ()),
        _ResponseCase("GET", "/admin", (("Host", "localhost:2456"),)),
        _ResponseCase(
            "GET",
            "/admin",
            (("Origin", "https://127.0.0.1:2456"),),
        ),
        _ResponseCase(
            "OPTIONS",
            "/admin/api/v1/overview",
            (("Origin", "http://127.0.0.1:2456"),),
        ),
        _ResponseCase("GET", "/not-a-route", ()),
    ],
)
def test_every_boundary_response_class_omits_all_cors_grants(
    client: TestClient,
    state: FakeAdminState,
    case: _ResponseCase,
) -> None:
    # Given: a public, auth, rejection, OPTIONS, or unknown-route request class.
    headers = Headers((*_admin_headers(state), *case.headers))

    # When: the response completes through the same middleware boundary.
    response = client.request(case.method, case.path, headers=headers)

    # Then: no Access-Control response field grants cross-origin access.
    assert not any(
        name.lower().startswith("access-control-") for name, _ in response.headers.multi_items()
    )
