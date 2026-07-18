"""Fail-closed loader for explicitly mapped package resources."""

from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files as _resource_files
from types import MappingProxyType
from typing import Final


class WebResource(StrEnum):
    """Closed set of web resources served by the application."""

    ADMIN_DOCUMENT = "admin-document"
    ADMIN_STYLESHEET = "admin-stylesheet"
    ADMIN_SCRIPT = "admin-script"
    FAVICON = "favicon"
    SHOWCASE_DOCUMENT = "showcase-document"
    SHOWCASE_STYLESHEET = "showcase-stylesheet"
    SHOWCASE_SCRIPT = "showcase-script"


@dataclass(frozen=True, slots=True)
class _ResourceLocation:
    package: str
    filename: str


_RESOURCE_LOCATIONS: Final = MappingProxyType(
    {
        WebResource.ADMIN_DOCUMENT: _ResourceLocation(
            package="nvidia_build_lb.web.templates",
            filename="admin.html",
        ),
        WebResource.ADMIN_STYLESHEET: _ResourceLocation(
            package="nvidia_build_lb.web.static",
            filename="admin.css",
        ),
        WebResource.ADMIN_SCRIPT: _ResourceLocation(
            package="nvidia_build_lb.web.static",
            filename="admin.js",
        ),
        WebResource.FAVICON: _ResourceLocation(
            package="nvidia_build_lb.web.static",
            filename="favicon.svg",
        ),
        WebResource.SHOWCASE_DOCUMENT: _ResourceLocation(
            package="nvidia_build_lb.web.templates",
            filename="showcase.html",
        ),
        WebResource.SHOWCASE_STYLESHEET: _ResourceLocation(
            package="nvidia_build_lb.web.static",
            filename="showcase.css",
        ),
        WebResource.SHOWCASE_SCRIPT: _ResourceLocation(
            package="nvidia_build_lb.web.static",
            filename="showcase.js",
        ),
    }
)


def load_web_resource(resource: WebResource) -> str:
    """Read one allowlisted UTF-8 package resource without a fallback path."""
    location = _RESOURCE_LOCATIONS[resource]
    return _resource_files(location.package).joinpath(location.filename).read_text(encoding="utf-8")
