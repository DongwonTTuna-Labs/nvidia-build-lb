from importlib.resources.abc import Traversable
from xml.etree.ElementTree import fromstring

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
        (WebResource.FAVICON, "<svg"),
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


def test_favicon_is_the_exact_passive_neutral_routing_graphic() -> None:
    source = load_web_resource(WebResource.FAVICON)
    expected = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">\n'
        "  <title>NVIDIA Build LB</title>\n"
        '  <rect width="64" height="64" rx="8" fill="#171C20" />\n'
        '  <path d="M32 18v12M20 30h24M20 30v14M44 30v14" fill="none" '
        'stroke="#C1C7C0" stroke-width="6" stroke-linecap="round" '
        'stroke-linejoin="round" />\n'
        "</svg>\n"
    )

    root = fromstring(source)  # noqa: S314 - the exact package resource is not untrusted input.
    namespace = "{http://www.w3.org/2000/svg}"
    children = list(root)

    assert source == expected
    assert root.tag == f"{namespace}svg"
    assert root.attrib == {"viewBox": "0 0 64 64"}
    assert [child.tag for child in children] == [
        f"{namespace}title",
        f"{namespace}rect",
        f"{namespace}path",
    ]
    assert children[0].attrib == {}
    assert children[0].text == "NVIDIA Build LB"
    assert children[1].attrib == {
        "width": "64",
        "height": "64",
        "rx": "8",
        "fill": "#171C20",
    }
    assert children[2].attrib == {
        "d": "M32 18v12M20 30h24M20 30v14M44 30v14",
        "fill": "none",
        "stroke": "#C1C7C0",
        "stroke-width": "6",
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
    }


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
    assert application.url_path_for("_get_favicon") == "/assets/favicon.svg"
    assert application.url_path_for("_health") == "/health"
