import pytest
from fastapi.testclient import TestClient

from nvidia_build_lb.main import create_app

_CSP = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'"
)
_POLICY = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "content-security-policy": _CSP,
}


@pytest.mark.parametrize(
    ("path", "media_type"),
    [
        ("/showcase", "text/html"),
        ("/assets/showcase.css", "text/css"),
        ("/assets/showcase.js", "text/javascript"),
        ("/assets/favicon.svg", "image/svg+xml"),
    ],
)
def test_exact_get_route_returns_packaged_content_when_requested(
    showcase_client: TestClient,
    path: str,
    media_type: str,
) -> None:
    # Given: the import-safe application has started with no runtime services.
    # When: the client requests one explicitly registered showcase resource.
    response = showcase_client.get(path)

    # Then: a non-empty response has the route's exact media family.
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media_type)
    assert response.content


@pytest.mark.parametrize(
    "path",
    ["/showcase", "/assets/showcase.css", "/assets/showcase.js", "/assets/favicon.svg"],
)
def test_showcase_response_has_exact_security_policy_when_served(
    showcase_client: TestClient,
    path: str,
) -> None:
    # Given: an explicitly registered showcase route.
    # When: the route is served.
    response = showcase_client.get(path)

    # Then: all browser policies are exact and no CORS grant is added.
    assert {name: response.headers.get(name) for name in _POLICY} == _POLICY
    assert not any(name.lower().startswith(b"access-control-") for name, _ in response.headers.raw)


@pytest.mark.parametrize(
    "path",
    [
        "/assets/showcase.css.map",
        "/assets/unknown.css",
        "/assets/templates/showcase.html",
    ],
)
def test_unknown_asset_is_not_resolved_when_name_is_unlisted(
    showcase_client: TestClient,
    path: str,
) -> None:
    # Given: a path outside the single explicit asset mapping.
    # When: the client requests that path.
    response = showcase_client.get(path)

    # Then: the application fails closed instead of mounting a directory.
    assert response.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/showcase", "/assets/showcase.css", "/assets/showcase.js", "/assets/favicon.svg"],
)
def test_showcase_route_rejects_post_when_only_get_is_allowed(
    showcase_client: TestClient,
    path: str,
) -> None:
    # Given: a known GET-only route.
    # When: a different method is used.
    response = showcase_client.post(path)

    # Then: framework method dispatch rejects it.
    assert response.status_code == 405


def test_application_starts_without_runtime_configuration_or_database_access() -> None:
    # Given: no settings, database, or secret bootstrap was requested.
    # When: a fresh application shell is created.
    app = create_app()

    # Then: the closed seven-resource shell plus degraded health exist without DB bootstrap.
    assert app.url_path_for("_get_showcase_document") == "/showcase"
    assert app.url_path_for("_get_showcase_stylesheet") == "/assets/showcase.css"
    assert app.url_path_for("_get_showcase_script") == "/assets/showcase.js"
    assert app.url_path_for("_get_favicon") == "/assets/favicon.svg"
    assert app.url_path_for("_get_admin_document") == "/admin"
    assert app.url_path_for("_health") == "/health"
    with TestClient(app, base_url="http://127.0.0.1:2456") as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.content == b'{"status":"degraded","ready":false}'
