"""Deterministic in-process NVIDIA ASGI wire fake."""

from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Self

import orjson
from pydantic import JsonValue
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route


@dataclass(frozen=True, slots=True)
class ObservedRequest:
    """A deliberately body- and header-free request observation."""

    method: str
    path: str
    body_size: int


@dataclass(frozen=True, slots=True)
class ScriptedResponse:
    """One deterministic response emitted without sockets or sleeps."""

    status_code: int
    media_type: str
    chunks: tuple[bytes, ...]
    headers: tuple[tuple[str, str], ...] = ()

    @classmethod
    def json(
        cls,
        payload: JsonValue,
        *,
        status_code: int = 200,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> Self:
        """Build a compact JSON response script."""
        return cls(
            status_code=status_code,
            media_type="application/json",
            chunks=(orjson.dumps(payload),),
            headers=headers,
        )

    @classmethod
    def text(
        cls,
        body: str,
        *,
        status_code: int,
        media_type: str = "text/plain",
        headers: tuple[tuple[str, str], ...] = (),
    ) -> Self:
        """Build a UTF-8 text response script."""
        return cls(
            status_code=status_code,
            media_type=media_type,
            chunks=(body.encode(),),
            headers=headers,
        )

    @classmethod
    def sse(cls, *chunks: bytes) -> Self:
        """Build an SSE script whose chunk boundaries remain deterministic."""
        return cls(status_code=200, media_type="text/event-stream", chunks=chunks)


class FakeNvidiaHarness:
    """Queue-backed ASGI fake that starts no service at construction time."""

    app: Starlette
    _responses: deque[ScriptedResponse]
    _observed: list[ObservedRequest]

    def __init__(self, responses: tuple[ScriptedResponse, ...] = ()) -> None:
        self._responses = deque(responses)
        self._observed = []
        self.app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["GET", "POST"])])

    @property
    def observed(self) -> tuple[ObservedRequest, ...]:
        """Return immutable, secret-safe request metadata."""
        return tuple(self._observed)

    @property
    def pending_count(self) -> int:
        """Return the number of response scripts not yet consumed."""
        return len(self._responses)

    def enqueue(self, response: ScriptedResponse) -> None:
        """Append one response without performing I/O."""
        self._responses.append(response)

    async def _handle(self, request: Request) -> Response:
        body = await request.body()
        self._observed.append(
            ObservedRequest(
                method=request.method,
                path=request.url.path,
                body_size=len(body),
            )
        )
        if not self._responses:
            return Response(
                content=b'{"error":"fake response queue exhausted"}',
                status_code=500,
                media_type="application/json",
            )

        scripted = self._responses.popleft()
        if len(scripted.chunks) == 1:
            response: Response = Response(
                content=scripted.chunks[0],
                status_code=scripted.status_code,
                media_type=scripted.media_type,
            )
        else:
            response = StreamingResponse(
                self._iterate(scripted.chunks),
                status_code=scripted.status_code,
                media_type=scripted.media_type,
            )
        response.raw_headers.extend(
            (name.encode("ascii"), value.encode("ascii")) for name, value in scripted.headers
        )
        return response

    @staticmethod
    async def _iterate(chunks: tuple[bytes, ...]) -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk
