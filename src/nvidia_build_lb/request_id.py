"""One opaque request identifier shared by auth, routes, errors, and logs."""

from typing import Final
from uuid import uuid4

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send

_STATE_ATTRIBUTE: Final = "nvidia_build_lb_request_id"


def request_id_from(request: Request) -> str:
    """Return the middleware-provided opaque identifier."""
    value = getattr(request.state, _STATE_ATTRIBUTE, None)
    if not isinstance(value, str) or not value:
        raise RuntimeError
    return value


class RequestIdMiddleware:
    """Create one server-owned request ID after Host and Origin validation."""

    _app: ASGIApp

    def __init__(self, app: ASGIApp) -> None:
        """Bind the inner application."""
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Publish one opaque identifier and preserve the raw ASGI call path."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        setattr(request.state, _STATE_ATTRIBUTE, uuid4().hex)
        await self._app(scope, receive, send)
