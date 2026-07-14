"""Shared sanitized NVIDIA client, response, and transport-failure types."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar, Protocol, override

import anyio

from nvidia_build_lb.outcomes import TransportErrorCode
from nvidia_build_lb.representations import RawResponse
from nvidia_build_lb.request_wire import NvidiaWireRequest

_TRANSPORT_ERROR = "nvidia_transport_error"


class NvidiaResponse(RawResponse, Protocol):
    """Sanitized response surface owned by the Todo 3 adapter."""

    status_code: int
    raw_headers: tuple[tuple[bytes, bytes], ...]

    async def aclose(self) -> None:
        """Retire or release the response stream exactly once."""
        ...


class NvidiaClient(Protocol):
    """Send one immutable request with no redirect or retry helper."""

    async def send(
        self,
        request: NvidiaWireRequest,
        *,
        timeout_seconds: float,
    ) -> NvidiaResponse:
        """Return one sanitized raw response."""
        ...


class OwnedNvidiaResponse:
    """Idempotent shielded ownership boundary around any client response."""

    __slots__: ClassVar[tuple[str, ...]] = (
        "_close_lock",
        "_closed",
        "_response",
        "raw_headers",
        "status_code",
    )
    _close_lock: anyio.Lock
    _closed: bool
    _response: NvidiaResponse
    raw_headers: tuple[tuple[bytes, bytes], ...]
    status_code: int

    def __init__(self, response: NvidiaResponse) -> None:
        """Take sole logical ownership of one sanitized response."""
        self._response = response
        self.status_code = response.status_code
        self.raw_headers = response.raw_headers
        self._close_lock = anyio.Lock()
        self._closed = False

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        """Delegate the one raw iterator without exposing the private response."""
        async for chunk in self._response.aiter_raw():
            yield chunk

    async def aclose(self) -> None:
        """Complete physical close once without defeating an outer deadline."""
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            await self._response.aclose()


@dataclass(slots=True)
class NvidiaTransportError(Exception):
    """Retain only a closed safe class and request-byte count."""

    code: TransportErrorCode
    request_bytes_sent: int

    @override
    def __str__(self) -> str:
        """Return one safe fixed code."""
        return _TRANSPORT_ERROR
