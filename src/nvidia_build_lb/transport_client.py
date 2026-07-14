"""Final no-log, no-cookie httpx2 client and response sanitization hooks."""

import time
from typing import final, override

from nvidia_build_lb.pinned_httpx import BoundAsyncStream, httpx2, request_context
from nvidia_build_lb.pinned_runtime import TransportBoundaryGuard, load_isolated_runtime
from nvidia_build_lb.transport_common import (
    PUBLIC_RESPONSE_HEADERS,
    RESPONSE_RAW_HEADERS_EXTENSION,
    VALIDATABLE_RESPONSE_HEADERS,
    PinnedTransportDriftError,
    RejectAllCookies,
)


@final
class PinnedNvidiaAsyncClient(httpx2.AsyncClient):
    """Final custom client that omits provider logging and cookie access."""

    _guard: TransportBoundaryGuard

    def __init__(self, transport: httpx2.AsyncBaseTransport) -> None:
        """Construct the final client with fixed options and guarded hooks."""
        self._guard = TransportBoundaryGuard(load_isolated_runtime().module_names)
        super().__init__(
            headers={},
            cookies=None,
            verify=True,
            http1=True,
            http2=False,
            proxy=None,
            mounts=None,
            timeout=httpx2.Timeout(connect=5.0, pool=2.0, read=120.0, write=15.0),
            follow_redirects=False,
            limits=httpx2.Limits(
                max_connections=32,
                max_keepalive_connections=16,
                keepalive_expiry=30.0,
            ),
            event_hooks={
                "request": [self._guard_request_hook],
                "response": [self._guard_response_hook],
            },
            transport=transport,
            trust_env=False,
        )
        self.cookies = RejectAllCookies()
        self._guard.observe()

    async def _guard_request_hook(self, _request: httpx2.Request) -> None:
        self._guard.observe()

    async def _guard_response_hook(self, response: httpx2.Response) -> None:
        self._guard.observe()
        await sanitize_response_hook(response)
        self._guard.observe()

    @override
    async def _send_single_request(self, request: httpx2.Request) -> httpx2.Response:
        self._guard.observe()
        transport = self._transport_for_url(request.url)
        start = time.perf_counter()
        if not isinstance(request.stream, httpx2.AsyncByteStream):
            raise PinnedTransportDriftError
        with request_context(request=request):
            response = await transport.handle_async_request(request)
        self._guard.observe()
        if not isinstance(response.stream, httpx2.AsyncByteStream):
            raise PinnedTransportDriftError
        response.request = request
        response.stream = BoundAsyncStream(response.stream, start=start)
        self.cookies.extract_cookies(response)
        response.default_encoding = self._default_encoding
        self._guard.observe()
        return response

    @override
    async def aclose(self) -> None:
        self._guard.observe()
        await super().aclose()
        self._guard.observe()


async def sanitize_response_hook(response: httpx2.Response) -> None:
    """Retain only typed validation inputs and safe public response headers."""
    raw_headers = tuple(
        (name, value)
        for name, value in response.headers.raw
        if name.lower() in VALIDATABLE_RESPONSE_HEADERS
    )
    public_headers = tuple(
        (canonical_name, value)
        for canonical_name in PUBLIC_RESPONSE_HEADERS
        for name, value in raw_headers
        if name.lower() == canonical_name
    )
    response.headers = httpx2.Headers(public_headers)
    if "reason_phrase" in response.extensions:
        del response.extensions["reason_phrase"]
    response.extensions[RESPONSE_RAW_HEADERS_EXTENSION] = raw_headers
