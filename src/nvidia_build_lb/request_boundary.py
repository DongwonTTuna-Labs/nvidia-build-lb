"""Ordered Host, Origin, and global OPTIONS request boundary."""

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final
from uuid import uuid4

import orjson
from fastapi import Request, Response
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_ACCEPTED_HOST: Final = "127.0.0.1:2456"
_ACCEPTED_ORIGIN: Final = "http://127.0.0.1:2456"
_CSP: Final = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'"
)
SECURITY_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _CSP,
    }
)


def _single_exact(values: Sequence[str], expected: str) -> bool:
    return len(values) == 1 and "," not in values[0] and values[0] == expected


def _admin_surface(path: str) -> bool:
    return path in {"/admin", "/showcase"} or path.startswith(("/admin/", "/assets/"))


def apply_security_policy(response: Response, path: str) -> Response:
    """Apply the closed browser/admin header policy to one response."""
    if _admin_surface(path):
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
    return response


def _forbidden(path: str, code: str, message: str) -> Response:
    response = Response(
        status_code=403,
        content=orjson.dumps(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": uuid4().hex,
                }
            }
        ),
        media_type="application/json",
    )
    return apply_security_policy(response, path)


def _security_policy_message(message: Message) -> Message:
    if message["type"] != "http.response.start":
        return message
    headers = MutableHeaders(scope=message)
    for name, value in SECURITY_HEADERS.items():
        headers[name] = value
    return message


class RequestBoundaryMiddleware:
    """Terminate Host and Origin failures before auth or route dispatch."""

    _app: ASGIApp

    def __init__(self, app: ASGIApp) -> None:
        """Bind the request boundary around the inner application."""
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Apply the fixed boundary order and global OPTIONS terminal."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        path = request.url.path
        if not _single_exact(request.headers.getlist("host"), _ACCEPTED_HOST):
            response = _forbidden(path, "host_forbidden", "request host is forbidden")
            await response(scope, receive, send)
            return
        origins = request.headers.getlist("origin")
        if origins and not _single_exact(origins, _ACCEPTED_ORIGIN):
            response = _forbidden(path, "origin_forbidden", "request origin is forbidden")
            await response(scope, receive, send)
            return
        if request.method == "OPTIONS":
            response = apply_security_policy(Response(status_code=405), path)
            await response(scope, receive, send)
            return

        if not _admin_surface(path):
            await self._app(scope, receive, send)
            return

        async def send_with_security_policy(message: Message) -> None:
            await send(_security_policy_message(message))

        await self._app(scope, receive, send_with_security_policy)
