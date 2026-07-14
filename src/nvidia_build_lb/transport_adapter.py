"""NVIDIA wire-request adapter over the pinned shared client."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar, Final, Protocol, assert_never, override, runtime_checkable

import anyio
from pydantic import ConfigDict, TypeAdapter, ValidationError

from nvidia_build_lb.nvidia_types import NvidiaResponse, NvidiaTransportError
from nvidia_build_lb.outcomes import TransportErrorCode
from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.request_wire import NvidiaAuthority, NvidiaWireRequest
from nvidia_build_lb.transport_client import PinnedNvidiaAsyncClient
from nvidia_build_lb.transport_common import (
    ORIGIN_BASE_URL,
    POLL_BASE_URL,
    RESPONSE_RAW_HEADERS_EXTENSION,
    PinnedTransportDriftError,
)
from nvidia_build_lb.transport_h1 import PinnedH1Transport

_RAW_HEADERS_ADAPTER: TypeAdapter[tuple[tuple[bytes, bytes], ...]] = TypeAdapter(
    tuple[tuple[bytes, bytes], ...],
    config=ConfigDict(strict=True),
)
_HTTPX_ERROR_CODES: Final[tuple[tuple[type[httpx2.RequestError], TransportErrorCode], ...]] = (
    (httpx2.PoolTimeout, TransportErrorCode.POOL_TIMEOUT),
    (httpx2.ConnectTimeout, TransportErrorCode.CONNECT_TIMEOUT),
    (httpx2.ReadTimeout, TransportErrorCode.READ_TIMEOUT),
    (httpx2.WriteTimeout, TransportErrorCode.WRITE_TIMEOUT),
    (httpx2.ConnectError, TransportErrorCode.CONNECT_ERROR),
    (httpx2.ReadError, TransportErrorCode.READ_ERROR),
    (httpx2.WriteError, TransportErrorCode.WRITE_ERROR),
    (httpx2.UnsupportedProtocol, TransportErrorCode.UNSUPPORTED_PROTOCOL),
    (httpx2.LocalProtocolError, TransportErrorCode.LOCAL_PROTOCOL_ERROR),
    (httpx2.RemoteProtocolError, TransportErrorCode.REMOTE_PROTOCOL_ERROR),
    (httpx2.ProtocolError, TransportErrorCode.PROTOCOL_ERROR),
    (httpx2.TimeoutException, TransportErrorCode.TIMEOUT),
    (httpx2.NetworkError, TransportErrorCode.NETWORK_ERROR),
)


@runtime_checkable
class _ResponseExtensions(Protocol):
    def pop(self, key: str, default: object = None) -> object:
        """Remove and return one extension without widening to Any."""
        ...


class SanitizedResponse(NvidiaResponse):
    """Expose only safe status, header metadata, raw bytes, and close."""

    __slots__: ClassVar[tuple[str, ...]] = (
        "_close_lock",
        "_closed",
        "_response",
        "raw_headers",
        "status_code",
    )

    _close_lock: anyio.Lock
    _closed: bool
    _response: httpx2.Response
    raw_headers: tuple[tuple[bytes, bytes], ...]
    status_code: int

    def __init__(
        self,
        response: httpx2.Response,
        raw_headers: tuple[tuple[bytes, bytes], ...],
    ) -> None:
        """Take ownership of one private httpx2 response."""
        self._response = response
        self.status_code = response.status_code
        self.raw_headers = raw_headers
        self._close_lock = anyio.Lock()
        self._closed = False

    @override
    async def aiter_raw(self) -> AsyncIterator[bytes]:
        failure: NvidiaTransportError | None = None
        try:
            async for chunk in self._response.aiter_raw():
                yield chunk
        except httpx2.RequestError as error:
            failure = NvidiaTransportError(_transport_error_code(error), 1)
        if failure is not None:
            raise failure

    @override
    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            failure: NvidiaTransportError | None = None
            try:
                await self._response.aclose()
            except httpx2.RequestError as error:
                self._closed = True
                failure = NvidiaTransportError(_transport_error_code(error), 1)
            else:
                self._closed = True
            if failure is not None:
                raise failure


@dataclass(frozen=True, slots=True)
class SanitizedAsyncClient:
    """Adapt immutable NVIDIA requests to one pinned shared client."""

    _client: PinnedNvidiaAsyncClient = field(repr=False)

    @classmethod
    def create(cls) -> "SanitizedAsyncClient":
        """Construct the production pinned H1 client."""
        return cls(PinnedNvidiaAsyncClient(PinnedH1Transport()))

    @classmethod
    def from_transport_for_test(
        cls,
        transport: httpx2.AsyncBaseTransport,
    ) -> "SanitizedAsyncClient":
        """Construct against one explicit deterministic test transport."""
        return cls(PinnedNvidiaAsyncClient(transport))

    async def send(
        self,
        request: NvidiaWireRequest,
        *,
        timeout_seconds: float,
    ) -> NvidiaResponse:
        """Send once with clipped timeouts and return a sanitized response."""
        if timeout_seconds <= 0:
            raise PinnedTransportDriftError
        timeout = {
            "connect": min(5.0, timeout_seconds),
            "pool": min(2.0, timeout_seconds),
            "read": min(120.0, timeout_seconds),
            "write": min(15.0, timeout_seconds),
        }
        httpx_request = httpx2.Request(
            request.method,
            f"{_base_url(request.authority)}{request.path}",
            headers=request.application_headers(),
            content=request.body,
            extensions={"timeout": timeout},
        )
        response: httpx2.Response | None = None
        failure: NvidiaTransportError | None = None
        try:
            response = await self._client.send(
                httpx_request,
                stream=True,
                follow_redirects=False,
            )
        except httpx2.RequestError as error:
            code = _transport_error_code(error)
            request_bytes_sent = 0 if code in _SAFE_BEFORE_SEND_CODES else len(request.body or b"")
            failure = NvidiaTransportError(code, request_bytes_sent)
        if failure is not None:
            raise failure
        if response is None:
            raise PinnedTransportDriftError
        extensions = _object_boundary(response.extensions)
        if not isinstance(extensions, _ResponseExtensions):
            await response.aclose()
            raise PinnedTransportDriftError
        raw_value = extensions.pop(RESPONSE_RAW_HEADERS_EXTENSION, None)
        try:
            raw_headers = _validated_raw_headers(raw_value)
        except PinnedTransportDriftError:
            await response.aclose()
            raise
        return SanitizedResponse(response, raw_headers)

    async def aclose(self) -> None:
        """Close the application-lifetime client."""
        await self._client.aclose()


_SAFE_BEFORE_SEND_CODES: Final = frozenset(
    {
        TransportErrorCode.POOL_TIMEOUT,
        TransportErrorCode.CONNECT_TIMEOUT,
        TransportErrorCode.CONNECT_ERROR,
    }
)


def _validated_raw_headers(value: object) -> tuple[tuple[bytes, bytes], ...]:
    raw_headers: tuple[tuple[bytes, bytes], ...] = ()
    try:
        raw_headers = _RAW_HEADERS_ADAPTER.validate_python(value)
    except ValidationError:
        invalid = True
    else:
        invalid = False
    if invalid:
        raise PinnedTransportDriftError
    return raw_headers


def _transport_error_code(error: httpx2.RequestError) -> TransportErrorCode:
    for error_type, code in _HTTPX_ERROR_CODES:
        if isinstance(error, error_type):
            return code
    return TransportErrorCode.PROTOCOL_ERROR


def _object_boundary(value: object) -> object:
    return value


def _base_url(authority: NvidiaAuthority) -> str:
    match authority:
        case NvidiaAuthority.ORIGIN:
            return ORIGIN_BASE_URL
        case NvidiaAuthority.POLL:
            return POLL_BASE_URL
        case _:
            assert_never(authority)
