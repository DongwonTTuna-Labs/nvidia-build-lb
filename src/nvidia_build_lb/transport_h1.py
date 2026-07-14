"""Guarded stream ownership and the application-lifetime pinned H1 transport."""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from typing import override

import anyio

from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.pinned_runtime import (
    PinnedRuntimeDriftError,
    TransportBoundaryGuard,
    load_isolated_runtime,
)
from nvidia_build_lb.transport_common import (
    ALLOWED_REQUEST_EXTENSIONS,
    PinnedTransportDriftError,
)
from nvidia_build_lb.transport_core import (
    CoreAsyncStream,
    CoreRequest,
    CoreResponse,
    TransportConfiguration,
    TransportCore,
    load_transport_core,
    mapped_error,
)
from nvidia_build_lb.transport_h1_helpers import (
    bounded_transport_cleanup,
    retire_raw_stream,
    timeout_extension,
)

_TRANSPORT_CLOSE_TIMEOUT_SECONDS = 1.0


class PinnedH1AsyncResponseStream(httpx2.AsyncByteStream):
    """Single-iterator guarded wrapper around one isolated H1 response stream."""

    _closed: bool
    _close_lock: anyio.Lock
    _core: TransportCore
    _deregister: Callable[["PinnedH1AsyncResponseStream"], None]
    _guard: TransportBoundaryGuard
    _iterated: bool
    _request: httpx2.Request
    _stream: CoreAsyncStream

    def __init__(
        self,
        *,
        stream: CoreAsyncStream,
        core: TransportCore,
        guard: TransportBoundaryGuard,
        request: httpx2.Request,
        deregister: Callable[["PinnedH1AsyncResponseStream"], None],
    ) -> None:
        """Take ownership of one isolated response stream."""
        self._stream = stream
        self._core = core
        self._guard = guard
        self._request = request
        self._deregister = deregister
        self._iterated = False
        self._closed = False
        self._close_lock = anyio.Lock()

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._iterated:
            raise PinnedTransportDriftError
        self._iterated = True
        iterator = self._stream.__aiter__()
        while True:
            self._guard.observe()
            chunk: bytes | None = None
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                self._guard.observe()
                return
            except Exception as error:  # noqa: BLE001 - closed core exception surface.
                failure = mapped_error(error, self._request, self._core)
            else:
                failure = None
            if failure is not None:
                raise failure
            if chunk is None:
                raise PinnedTransportDriftError
            self._guard.observe()
            yield chunk

    @override
    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._guard.observe()
            failure: Exception | None = None
            try:
                await self._stream.aclose()
            except Exception as error:  # noqa: BLE001 - closed core exception surface.
                self._closed = True
                self._deregister(self)
                failure = mapped_error(error, self._request, self._core)
            else:
                self._closed = True
            if failure is not None:
                raise failure
            self._deregister(self)
            self._guard.observe()


class PinnedH1Transport(httpx2.AsyncBaseTransport):
    """H1-only, no-proxy, zero-retry transport using isolated source."""

    _closed: bool
    _core: TransportCore
    _guard: TransportBoundaryGuard
    _streams: set[PinnedH1AsyncResponseStream]
    configuration: TransportConfiguration

    def __init__(self, *, core: TransportCore | None = None) -> None:
        """Construct the verified fixed pool without default helpers."""
        runtime = load_isolated_runtime()
        self._guard = TransportBoundaryGuard(runtime.module_names)
        self._core = load_transport_core() if core is None else core
        self.configuration = TransportConfiguration()
        self._streams = set()
        self._closed = False
        self._guard.observe()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._guard.observe()
        if self._closed or set(request.extensions) - ALLOWED_REQUEST_EXTENSIONS:
            raise PinnedTransportDriftError
        core_url = self._core.url_factory(
            scheme=request.url.raw_scheme,
            host=request.url.raw_host,
            port=request.url.port,
            target=request.url.raw_path,
        )
        core_request = self._core.request_factory(
            request.method,
            core_url,
            headers=tuple(request.headers.raw),
            content=request.stream,
            extensions=timeout_extension(request.extensions),
        )
        core_response = await _send_core(self._core, core_request, request)
        stream: PinnedH1AsyncResponseStream | None = None
        construction_error: BaseException
        try:
            self._guard.observe()
            http_version = _exact_h1_version(core_response.extensions)
            stream = PinnedH1AsyncResponseStream(
                stream=core_response.stream,
                core=self._core,
                guard=self._guard,
                request=request,
                deregister=self._discard_stream,
            )
            self._streams.add(stream)
            return httpx2.Response(
                core_response.status,
                headers=core_response.headers,
                stream=stream,
                extensions={"http_version": http_version},
            )
        except BaseException as error:  # noqa: BLE001 - preserve cancellation after cleanup.
            construction_error = error
        with suppress(BaseException):
            if stream is None:
                await retire_raw_stream(core_response.stream, request, self._core)
            else:
                await stream.aclose()
        if isinstance(
            construction_error,
            (PinnedRuntimeDriftError, PinnedTransportDriftError),
        ):
            raise construction_error
        if isinstance(construction_error, Exception):
            raise PinnedTransportDriftError
        raise construction_error

    @override
    async def aclose(self) -> None:
        if self._closed:
            return
        self._guard.observe()
        self._closed = True
        primary_error: BaseException | None = None
        for stream in tuple(self._streams):
            error = await bounded_transport_cleanup(
                stream.aclose,
                _TRANSPORT_CLOSE_TIMEOUT_SECONDS,
            )
            if primary_error is None and error is not None:
                primary_error = error
        pool_error = await bounded_transport_cleanup(
            self._core.pool.aclose,
            _TRANSPORT_CLOSE_TIMEOUT_SECONDS,
            sanitize_exception=True,
        )
        if primary_error is None and pool_error is not None:
            primary_error = pool_error
        try:
            self._guard.observe()
        except BaseException as error:  # noqa: BLE001 - preserve an earlier cleanup failure.
            if primary_error is None:
                primary_error = error
        if primary_error is not None:
            raise primary_error from None

    def _discard_stream(self, stream: PinnedH1AsyncResponseStream) -> None:
        self._streams.discard(stream)


async def _send_core(
    core: TransportCore,
    core_request: CoreRequest,
    request: httpx2.Request,
) -> CoreResponse:
    response: CoreResponse | None = None
    failure: Exception | None = None
    try:
        response = await core.pool.handle_async_request(core_request)
    except Exception as error:  # noqa: BLE001 - closed core exception surface.
        failure = mapped_error(error, request, core)
    if failure is not None:
        raise failure
    if response is None:
        raise PinnedTransportDriftError
    return response


def _exact_h1_version(extensions: Mapping[str, object]) -> bytes:
    """Require the isolated H1 pool to report its exact response protocol."""
    http_version = extensions.get("http_version")
    if type(http_version) is not bytes or http_version != b"HTTP/1.1":
        raise PinnedTransportDriftError
    return http_version
