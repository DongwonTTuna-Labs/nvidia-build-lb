"""Terminate safe pre-response failures without hiding downstream send errors."""

from dataclasses import dataclass
from typing import Final, override
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from starlette.types import Message, Receive, Scope, Send

from nvidia_build_lb.request_boundary import apply_security_policy
from nvidia_build_lb.request_id import request_id_from
from nvidia_build_lb.schemas import ErrorEnvelope

_RESPONSE_STARTED_ATTRIBUTE: Final = "nvidia_build_lb_response_started"
_SAFE_INTERNAL_ERROR_ATTRIBUTE: Final = "nvidia_build_lb_safe_internal_error"


def response_has_started(request: Request) -> bool:
    """Return whether an HTTP response start reached the real server send."""
    return getattr(request.state, _RESPONSE_STARTED_ATTRIBUTE, False) is True


def select_safe_internal_error(request: Request) -> None:
    """Mark a pre-response exception whose fixed 500 may be safely terminated."""
    setattr(request.state, _SAFE_INTERNAL_ERROR_ATTRIBUTE, True)


def safe_internal_error_response(request: Request) -> Response:
    """Build a secret-free internal error with the applicable admin policy."""
    try:
        request_id = request_id_from(request)
    except RuntimeError:
        request_id = uuid4().hex
    response = Response(
        status_code=500,
        content=ErrorEnvelope.from_safe_parts(
            "internal_server_error",
            "internal server error",
            request_id,
        ).model_dump_json(),
        media_type="application/json",
    )
    return apply_security_policy(response, request.url.path)


def _safe_internal_error_selected(scope: Scope) -> bool:
    request = Request(scope)
    return getattr(request.state, _SAFE_INTERNAL_ERROR_ATTRIBUTE, False) is True


def _mark_response_started(scope: Scope) -> None:
    setattr(Request(scope).state, _RESPONSE_STARTED_ATTRIBUTE, True)


@dataclass(slots=True)
class _ResponseDeliveryState:
    scope: Scope
    downstream_send: Send
    response_start_attempted: bool = False
    safe_response_complete: bool = False

    async def send(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.response_start_attempted = True
        await self.downstream_send(message)
        if message["type"] == "http.response.start":
            _mark_response_started(self.scope)
        elif (
            message["type"] == "http.response.body"
            and not message.get("more_body", False)
            and _safe_internal_error_selected(self.scope)
        ):
            self.safe_response_complete = True


async def _finish_exception(
    error: Exception,
    scope: Scope,
    receive: Receive,
    send: Send,
    state: _ResponseDeliveryState,
) -> None:
    if state.safe_response_complete:
        return
    if _safe_internal_error_selected(scope):
        raise error from None
    if state.response_start_attempted:
        raise error
    request = Request(scope, receive=receive)
    response = safe_internal_error_response(request)
    try:
        await response(scope, receive, send)
    except Exception as delivery_error:
        raise delivery_error from None


class SafeServerErrorFastAPI(FastAPI):
    """Suppress only a fully delivered fixed 500 before it reaches server logs."""

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Preserve send failures while terminating one completed safe error response."""
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return

        state = _ResponseDeliveryState(scope, send)
        try:
            await super().__call__(scope, receive, state.send)
        except Exception as error:  # noqa: BLE001 - outer server boundary classifies by delivery.
            await _finish_exception(error, scope, receive, send, state)
