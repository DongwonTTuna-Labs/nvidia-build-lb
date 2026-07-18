from collections.abc import Iterator
from gzip import compress

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from nvidia_build_lb.web.admin_resources import create_admin_resource_router
from nvidia_build_lb.web.resources import WebResource, load_web_resource

pytestmark = pytest.mark.ui_fake

_CSP = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'"
)
_EXPECTED_HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "content-security-policy": _CSP,
    "vary": "Accept-Encoding",
}


@pytest.fixture
def client() -> Iterator[TestClient]:
    application = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    application.include_router(create_admin_resource_router())
    with TestClient(application, base_url="http://127.0.0.1:2456") as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "media_type", "resource"),
    [
        ("/admin", "text/html", WebResource.ADMIN_DOCUMENT),
        ("/showcase", "text/html", WebResource.SHOWCASE_DOCUMENT),
        ("/assets/admin.css", "text/css", WebResource.ADMIN_STYLESHEET),
        ("/assets/admin.js", "text/javascript", WebResource.ADMIN_SCRIPT),
        ("/assets/favicon.svg", "image/svg+xml", WebResource.FAVICON),
        ("/assets/showcase.css", "text/css", WebResource.SHOWCASE_STYLESHEET),
        ("/assets/showcase.js", "text/javascript", WebResource.SHOWCASE_SCRIPT),
    ],
)
def test_closed_web_resource_route_serves_exact_policy(
    client: TestClient,
    path: str,
    media_type: str,
    resource: WebResource,
) -> None:
    # Given: one path from the closed route allowlist.
    # When: it is requested without authentication.
    response = client.get(path, headers={"Accept-Encoding": "identity"})

    # Then: package text and the complete response policy are exact.
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)
    assert response.content == load_web_resource(resource).encode()
    assert {name: response.headers[name] for name in _EXPECTED_HEADERS} == _EXPECTED_HEADERS
    assert "content-encoding" not in response.headers


@pytest.mark.parametrize(
    ("path", "resource"),
    [
        ("/admin", WebResource.ADMIN_DOCUMENT),
        ("/showcase", WebResource.SHOWCASE_DOCUMENT),
        ("/assets/admin.css", WebResource.ADMIN_STYLESHEET),
        ("/assets/admin.js", WebResource.ADMIN_SCRIPT),
        ("/assets/favicon.svg", WebResource.FAVICON),
        ("/assets/showcase.css", WebResource.SHOWCASE_STYLESHEET),
        ("/assets/showcase.js", WebResource.SHOWCASE_SCRIPT),
    ],
)
def test_closed_web_resource_route_negotiates_gzip_only_when_accepted(
    client: TestClient,
    path: str,
    resource: WebResource,
) -> None:
    expected = load_web_resource(resource).encode()

    response = client.get(path, headers={"Accept-Encoding": "br, gzip; q=0.7"})

    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["vary"] == "Accept-Encoding"
    assert int(response.headers["content-length"]) == len(
        compress(expected, compresslevel=6, mtime=0)
    )


@pytest.mark.parametrize(
    "accept_encoding",
    [
        "br",
        "gzip;q=0",
        "gzip;q=2",
        "gzip;q=nan",
        "gzip;q=.5",
        "gzip;q=+0.5",
        "gzip;q=1e-1",
        "gzip;q=0.1234",
        "gzip;foo=bar",
        "gzip;",
        "gzip;q=0.5;foo=bar",
        "gzip;q=0, *;q=1",
    ],
)
def test_closed_web_resource_route_rejects_unaccepted_or_invalid_gzip(
    client: TestClient,
    accept_encoding: str,
) -> None:
    expected = load_web_resource(WebResource.ADMIN_DOCUMENT).encode()

    response = client.get("/admin", headers={"Accept-Encoding": accept_encoding})

    assert response.status_code == 200
    assert response.content == expected
    assert "content-encoding" not in response.headers
    assert int(response.headers["content-length"]) == len(expected)


def test_closed_web_resource_route_combines_repeated_accept_encoding_fields(
    client: TestClient,
) -> None:
    expected = load_web_resource(WebResource.ADMIN_DOCUMENT).encode()

    response = client.get(
        "/admin",
        headers=[("Accept-Encoding", "br"), ("Accept-Encoding", "gzip;q=0.5")],
    )

    assert response.status_code == 200
    assert response.content == expected
    assert response.headers["content-encoding"] == "gzip"


def test_unknown_asset_fails_closed_without_generic_mount(client: TestClient) -> None:
    # Given: a filename outside the four asset routes.
    # When: the unknown asset is requested.
    response = client.get("/assets/admin.map")

    # Then: no filesystem or fallback route serves it.
    assert response.status_code == 404


def test_browser_resource_route_registry_is_the_exact_closed_seven() -> None:
    # Given: the production UI-only resource router.
    router = create_admin_resource_router()

    # When: registered HTTP paths and methods are projected.
    assert len(router.routes) == 7
    assert all(isinstance(route, APIRoute) for route in router.routes)
    routes = tuple(
        (route.path, tuple(sorted(route.methods or ())))
        for route in router.routes
        if isinstance(route, APIRoute)
    )

    # Then: only the seven named GET resources exist.
    assert routes == (
        ("/admin", ("GET",)),
        ("/assets/admin.css", ("GET",)),
        ("/assets/admin.js", ("GET",)),
        ("/assets/favicon.svg", ("GET",)),
        ("/showcase", ("GET",)),
        ("/assets/showcase.css", ("GET",)),
        ("/assets/showcase.js", ("GET",)),
    )
    assert sum(path.startswith("/assets/") for path, _ in routes) == 5
