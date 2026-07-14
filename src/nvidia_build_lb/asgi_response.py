"""Typed ASGI messages for committed JSON and live SSE responses."""

from collections.abc import Awaitable, Callable

from nvidia_build_lb.routing import RoutedJson

type AsgiMessage = dict[str, object]
type AsgiReceive = Callable[[], Awaitable[AsgiMessage]]
type AsgiSend = Callable[[AsgiMessage], Awaitable[None]]

_SSE_HEADERS = [(b"content-type", b"text/event-stream")]
_JSON_HEADERS = [(b"content-type", b"application/json")]


async def send_json_response(
    routed: RoutedJson,
    send: AsgiSend,
    *,
    requested_stream: bool,
) -> None:
    """Send committed JSON unchanged or convert it to data plus DONE SSE."""
    if not requested_stream:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": _JSON_HEADERS,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": routed.terminal.representation.raw,
                "more_body": False,
            }
        )
        return
    frames = routed.stream_frames
    if frames is None:
        raise RuntimeError
    await send(sse_response_start())
    await send(sse_response_body(frames[0], more_body=True))
    await send(sse_response_body(frames[1], more_body=False))


def sse_response_start() -> AsgiMessage:
    """Build the one SSE response-start message."""
    return {"type": "http.response.start", "status": 200, "headers": _SSE_HEADERS}


def sse_response_body(body: bytes, *, more_body: bool) -> AsgiMessage:
    """Build one ordinary or terminal SSE body message."""
    return {"type": "http.response.body", "body": body, "more_body": more_body}
