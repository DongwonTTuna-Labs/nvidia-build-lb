"""Response objects that preserve routing-owned ASGI stream semantics."""

from typing import final, override

from starlette.responses import Response
from starlette.types import Receive, Scope, Send

from nvidia_build_lb.active_routed_requests import ActiveRoutedRequestRegistry
from nvidia_build_lb.asgi_response import AsgiMessage
from nvidia_build_lb.routing import RoutedJson, RoutedStream
from nvidia_build_lb.streaming import ChatStreamResponder


@final
class RoutedChatResponse(Response):
    """Delegate response-start, body, disconnect, and terminal order."""

    _requested_stream: bool
    _responder: ChatStreamResponder
    _routed: RoutedJson | RoutedStream

    def __init__(
        self,
        routed: RoutedJson | RoutedStream,
        responder: ChatStreamResponder,
        *,
        requested_stream: bool,
    ) -> None:
        """Bind one routed result and its request-scoped responder."""
        super().__init__(status_code=200)
        self._routed = routed
        self._responder = responder
        self._requested_stream = requested_stream

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        del scope

        async def receive_message() -> AsgiMessage:
            return dict(await receive())

        async def send_message(message: AsgiMessage) -> None:
            await send(message)

        await self._responder.run(
            routed=self._routed,
            receive=receive_message,
            send=send_message,
            requested_stream=self._requested_stream,
        )


@final
class ActiveRoutedResponse(Response):
    """Keep one routed request registered through actual ASGI response completion."""

    _response: Response
    _active_requests: ActiveRoutedRequestRegistry
    _request_id: str

    def __init__(
        self,
        response: Response,
        active_requests: ActiveRoutedRequestRegistry,
        request_id: str,
    ) -> None:
        """Take ownership of the registry release after route dispatch."""
        super().__init__(status_code=response.status_code)
        self._response = response
        self._active_requests = active_requests
        self._request_id = request_id

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._response(scope, receive, send)
        finally:
            self._active_requests.release(self._request_id)
