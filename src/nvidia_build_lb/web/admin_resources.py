"""Exact administration shell and asset routes for later composition."""

from collections.abc import Mapping
from gzip import compress
from re import fullmatch
from types import MappingProxyType
from typing import Final

from fastapi import APIRouter, Request
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
        "Vary": "Accept-Encoding",
    }
)


def _quality(parameters: list[str]) -> float:
    if len(parameters) != 1:
        return 0.0
    name, separator, value = parameters[0].partition("=")
    if name.strip().lower() != "q":
        return 0.0
    if not separator:
        return 0.0
    normalized = value.strip()
    if fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", normalized) is None:
        return 0.0
    return float(normalized)


def _accepts_gzip(value: str | None) -> bool:
    explicit: list[float] = []
    wildcard: list[float] = []
    for item in (value or "").split(","):
        coding, *parameters = item.split(";")
        normalized = coding.strip().lower()
        if normalized not in {"gzip", "*"}:
            continue
        quality = 1.0 if not parameters else _quality(parameters)
        (explicit if normalized == "gzip" else wildcard).append(quality)
    selected = max(explicit) if explicit else max(wildcard, default=0.0)
    return selected > 0.0


def _accept_encoding(request: Request) -> str | None:
    values = request.headers.getlist("accept-encoding")
    return ",".join(values) if values else None


def _resource_response(
    resource: WebResource,
    media_type: str,
    accept_encoding: str | None,
) -> Response:
    content = load_web_resource(resource).encode()
    headers = dict(_ADMIN_HEADERS)
    if _accepts_gzip(accept_encoding):
        content = compress(content, compresslevel=6, mtime=0)
        headers["Content-Encoding"] = "gzip"
    return Response(
        content=content,
        media_type=media_type,
        headers=headers,
    )


def _get_admin_document(request: Request) -> Response:
    return _resource_response(
        WebResource.ADMIN_DOCUMENT,
        "text/html",
        _accept_encoding(request),
    )


def _get_admin_stylesheet(request: Request) -> Response:
    return _resource_response(
        WebResource.ADMIN_STYLESHEET,
        "text/css",
        _accept_encoding(request),
    )


def _get_admin_script(request: Request) -> Response:
    return _resource_response(
        WebResource.ADMIN_SCRIPT,
        "text/javascript",
        _accept_encoding(request),
    )


def _get_favicon(request: Request) -> Response:
    return _resource_response(
        WebResource.FAVICON,
        "image/svg+xml",
        _accept_encoding(request),
    )


def _get_showcase_document(request: Request) -> Response:
    return _resource_response(
        WebResource.SHOWCASE_DOCUMENT,
        "text/html",
        _accept_encoding(request),
    )


def _get_showcase_stylesheet(request: Request) -> Response:
    return _resource_response(
        WebResource.SHOWCASE_STYLESHEET,
        "text/css",
        _accept_encoding(request),
    )


def _get_showcase_script(request: Request) -> Response:
    return _resource_response(
        WebResource.SHOWCASE_SCRIPT,
        "text/javascript",
        _accept_encoding(request),
    )


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
        "/assets/favicon.svg",
        _get_favicon,
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
    router.add_api_route(
        "/assets/showcase.js",
        _get_showcase_script,
        methods=["GET"],
        include_in_schema=False,
        response_class=Response,
    )
    return router
