from importlib.resources.abc import Traversable

import pytest

import nvidia_build_lb.main as main_module
import nvidia_build_lb.web.admin_resources as admin_resource_module
import nvidia_build_lb.web.resources as resource_module
from nvidia_build_lb.web.resources import WebResource, load_web_resource


@pytest.mark.parametrize(
    ("resource", "marker"),
    [
        (WebResource.SHOWCASE_DOCUMENT, "<!doctype html>"),
        (WebResource.SHOWCASE_STYLESHEET, ":root"),
        (WebResource.SHOWCASE_SCRIPT, "syncShowcaseNavigation"),
    ],
)
def test_allowlisted_package_resource_is_read_when_requested(
    resource: WebResource,
    marker: str,
) -> None:
    # Given: one member of the closed resource allowlist.
    # When: the package loader reads it.
    content = load_web_resource(resource)

    # Then: the expected packaged artifact is returned as text.
    assert marker in content


def test_resource_name_fails_closed_when_it_is_not_allowlisted() -> None:
    # Given: a name absent from the resource enum.
    # When: the name is parsed at the closed boundary.
    with pytest.raises(ValueError, match="is not a valid WebResource"):
        _ = WebResource("unlisted-resource")

    # Then: no fallback path or wildcard resource value exists.


def test_resource_read_error_propagates_when_package_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: package lookup fails before a resource can be opened.
    def missing_package_files(anchor: str) -> Traversable:
        del anchor
        raise ModuleNotFoundError

    monkeypatch.setattr(resource_module, "_resource_files", missing_package_files)

    # When: the explicit loader is called.
    # Then: it fails closed instead of returning fallback content.
    with pytest.raises(ModuleNotFoundError):
        _ = load_web_resource(WebResource.SHOWCASE_DOCUMENT)


def test_application_creation_does_not_read_resource_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a loader double that would fail if startup read package content.
    def reject_resource_read(resource: WebResource) -> str:
        del resource
        raise AssertionError

    monkeypatch.setattr(admin_resource_module, "load_web_resource", reject_resource_read)

    # When: an application is created without handling a request.
    application = main_module.create_app()

    # Then: startup is import-safe and registers the closed shell plus degraded health.
    assert application.url_path_for("_get_showcase_document") == "/showcase"
    assert application.url_path_for("_get_showcase_stylesheet") == "/assets/showcase.css"
    assert application.url_path_for("_get_showcase_script") == "/assets/showcase.js"
    assert application.url_path_for("_health") == "/health"
