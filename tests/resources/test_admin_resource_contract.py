import pytest

from nvidia_build_lb.web.resources import WebResource, load_web_resource

pytestmark = pytest.mark.ui_fake


def test_admin_resources_are_closed_package_members_when_requested() -> None:
    # Given: the three exact Todo 4 browser resources.
    expected = {
        "admin-document": "<!doctype html>",
        "admin-stylesheet": ":root",
        "admin-script": "const",
    }

    # When: each closed wire name is parsed and loaded.
    observed = {
        resource_name: load_web_resource(WebResource(resource_name)) for resource_name in expected
    }

    # Then: every artifact is packaged text with its expected surface marker.
    assert all(marker in observed[name] for name, marker in expected.items())


def test_admin_resource_allowlist_has_no_generic_path_when_enumerated() -> None:
    # Given: the complete resource enum.
    values = {resource.value for resource in WebResource}

    # When: the Todo 4 names are compared with the existing showcase resources.
    # Then: only the six exact document and asset names exist.
    assert values == {
        "admin-document",
        "admin-stylesheet",
        "admin-script",
        "showcase-document",
        "showcase-stylesheet",
        "showcase-script",
    }
