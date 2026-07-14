"""Exact administration shell and asset routes for later composition."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from fastapi import APIRouter
from fastapi.responses import Response

from nvidia_build_lb.web.resources import WebResource, load_web_resource

_CONTENT_SECURITY_POLICY: Final = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'"
)
_ADMIN_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
    }
)


def _resource_response(resource: WebResource, media_type: str) -> Response:
    return Response(
        content=load_web_resource(resource),
        media_type=media_type,
        headers=_ADMIN_HEADERS,
    )


def _get_admin_document() -> Response:
    return _resource_response(WebResource.ADMIN_DOCUMENT, "text/html")


def _get_admin_stylesheet() -> Response:
    return _resource_response(WebResource.ADMIN_STYLESHEET, "text/css")


def _get_admin_script() -> Response:
    return _resource_response(WebResource.ADMIN_SCRIPT, "text/javascript")


def _get_showcase_document() -> Response:
    return _resource_response(WebResource.SHOWCASE_DOCUMENT, "text/html")


def _get_showcase_stylesheet() -> Response:
    return _resource_response(WebResource.SHOWCASE_STYLESHEET, "text/css")


def create_admin_resource_router() -> APIRouter:
    """Build the closed unauthenticated shell and asset route set."""
    router = APIRouter()
    router.add_api_route(
        "/admin",
        _get_admin_document,
        methods=["GET"],
        include_in_schema=False,
        response_class=Response,
    )
    router.add_api_route(
        "/assets/admin.css",
        _get_admin_stylesheet,
        methods=["GET"],
        include_in_schema=False,
        response_class=Response,
    )
    router.add_api_route(
        "/assets/admin.js",
        _get_admin_script,
        methods=["GET"],
        include_in_schema=False,
        response_class=Response,
    )
    router.add_api_route(
        "/showcase",
        _get_showcase_document,
        methods=["GET"],
        include_in_schema=False,
        response_class=Response,
    )
    router.add_api_route(
        "/assets/showcase.css",
        _get_showcase_stylesheet,
        methods=["GET"],
        include_in_schema=False,
        response_class=Response,
    )
    return router
