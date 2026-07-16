from collections.abc import Iterator

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
    response = client.get(path)

    # Then: package text and the complete response policy are exact.
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)
    assert response.content == load_web_resource(resource).encode()
    assert {name: response.headers[name] for name in _EXPECTED_HEADERS} == _EXPECTED_HEADERS


def test_unknown_asset_fails_closed_without_generic_mount(client: TestClient) -> None:
    # Given: a filename outside the four asset routes.
    # When: the unknown asset is requested.
    response = client.get("/assets/admin.map")

    # Then: no filesystem or fallback route serves it.
    assert response.status_code == 404


def test_browser_resource_route_registry_is_the_exact_closed_six() -> None:
    # Given: the production UI-only resource router.
    router = create_admin_resource_router()

    # When: registered HTTP paths and methods are projected.
    assert len(router.routes) == 6
    assert all(isinstance(route, APIRoute) for route in router.routes)
    routes = tuple(
        (route.path, tuple(sorted(route.methods or ())))
        for route in router.routes
        if isinstance(route, APIRoute)
    )

    # Then: only the six named GET resources exist.
    assert routes == (
        ("/admin", ("GET",)),
        ("/assets/admin.css", ("GET",)),
        ("/assets/admin.js", ("GET",)),
        ("/showcase", ("GET",)),
        ("/assets/showcase.css", ("GET",)),
        ("/assets/showcase.js", ("GET",)),
    )
    assert sum(path.startswith("/assets/") for path, _ in routes) == 4
