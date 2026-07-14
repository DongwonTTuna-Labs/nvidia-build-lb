"""Custom client suppresses logs/cookies and exposes only safe response headers."""

from collections.abc import AsyncIterator
from typing import Self, override

import anyio
import pytest
from anyio.lowlevel import checkpoint
from pydantic import SecretStr

from nvidia_build_lb.nvidia_types import NvidiaTransportError
from nvidia_build_lb.outcomes import TransportErrorCode
from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.request_wire import build_nvidia_request
from nvidia_build_lb.transport import SanitizedAsyncClient
from nvidia_build_lb.transport_adapter import SanitizedResponse

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_READ_TIMEOUT_MESSAGE = "synthetic read timeout"


class _ResponseStream(httpx2.AsyncByteStream):
    close_count: int

    def __init__(self) -> None:
        self.close_count = 0

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"unread"

    @override
    async def aclose(self) -> None:
        self.close_count += 1


class _CancellationStream(_ResponseStream):
    close_count: int
    close_finished: int

    def __init__(self) -> None:
        super().__init__()
        self.close_finished = 0

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        await checkpoint()
        self.close_finished += 1


class _ReadFailureStream(_ResponseStream):
    @override
    def __aiter__(self) -> AsyncIterator[bytes]:
        return _ReadFailureIterator()


class _CloseFailureStream(_ResponseStream):
    close_count: int

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        raise httpx2.ReadTimeout(_READ_TIMEOUT_MESSAGE)


class _ReadFailureIterator:
    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        raise httpx2.ReadTimeout(_READ_TIMEOUT_MESSAGE)


class _ScriptedTransport(httpx2.AsyncBaseTransport):
    calls: int
    requests: list[tuple[str, str, str, int | None, bytes]]
    stream: _ResponseStream
    status_code: int

    def __init__(self, *, status_code: int) -> None:
        self.calls = 0
        self.requests = []
        self.stream = _ResponseStream()
        self.status_code = status_code

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        self.requests.append(
            (
                request.method,
                request.url.scheme,
                request.url.host,
                request.url.port,
                request.url.raw_path,
            )
        )
        assert request.headers.get("cookie") is None
        headers = [
            (b"Retry-After", b"2"),
            (b"X-Secret", b"never-forward"),
            (b"Set-Cookie", b"poison=secret"),
        ]
        if self.status_code == 302:
            headers.append((b"Location", b"https://untrusted.invalid/secret"))
        return httpx2.Response(
            self.status_code,
            headers=headers,
            stream=self.stream,
            extensions={"reason_phrase": _ExplodingText(), "http_version": b"HTTP/1.1"},
        )

    @override
    async def aclose(self) -> None:
        return


class _ExplodingText:
    @override
    def __str__(self) -> str:
        raise AssertionError

    @override
    def __repr__(self) -> str:
        raise AssertionError


async def test_sanitized_client_drops_secret_headers_and_never_reads_error_body() -> None:
    transport = _ScriptedTransport(status_code=429)
    client = SanitizedAsyncClient.from_transport_for_test(transport)
    request = build_nvidia_request(
        credential=SecretStr("synthetic-secret"),
        body=b"{}",
    )

    response = await client.send(request, timeout_seconds=10.0)

    assert response.status_code == 429
    assert tuple((name.lower(), value) for name, value in response.raw_headers) == (
        (b"retry-after", b"2"),
    )
    assert transport.calls == 1
    assert transport.stream.close_count == 0
    await response.aclose()
    assert transport.stream.close_count == 1
    await client.aclose()


async def test_redirect_response_has_zero_followups_and_location_is_not_exposed() -> None:
    transport = _ScriptedTransport(status_code=302)
    client = SanitizedAsyncClient.from_transport_for_test(transport)
    request = build_nvidia_request(
        credential=SecretStr("synthetic-secret"),
        body=b"{}",
    )

    response = await client.send(request, timeout_seconds=10.0)

    assert response.status_code == 302
    assert transport.calls == 1
    assert all(name.lower() != b"location" for name, _value in response.raw_headers)
    await response.aclose()
    await client.aclose()


async def test_origin_and_poll_use_distinct_fixed_https_authorities() -> None:
    transport = _ScriptedTransport(status_code=200)
    client = SanitizedAsyncClient.from_transport_for_test(transport)
    credential = SecretStr("synthetic-secret")
    requests = (
        build_nvidia_request(credential=credential, body=b"{}"),
        build_nvidia_request(
            credential=credential,
            body=None,
            origin_request_id="request-202",
        ),
    )

    for request in requests:
        response = await client.send(request, timeout_seconds=10.0)
        await response.aclose()
    await client.aclose()

    assert transport.requests == [
        ("POST", "https", "integrate.api.nvidia.com", None, b"/v1/chat/completions"),
        (
            "GET",
            "https",
            "api.nvcf.nvidia.com",
            None,
            b"/v2/nvcf/pexec/status/request-202",
        ),
    ]
    assert transport.calls == 2


async def test_sanitized_response_close_respects_cancellation_without_losing_ownership() -> None:
    stream = _CancellationStream()
    response = SanitizedResponse(httpx2.Response(200, stream=stream), ())

    with anyio.CancelScope() as scope:
        scope.cancel()
        await response.aclose()
    assert stream.close_finished == 0
    await response.aclose()

    assert stream.close_count == 1
    assert stream.close_finished == 1


async def test_sanitized_response_maps_body_request_error_without_text() -> None:
    stream = _ReadFailureStream()
    response = SanitizedResponse(httpx2.Response(200, stream=stream), ())

    with pytest.raises(NvidiaTransportError) as captured:
        _ = [chunk async for chunk in response.aiter_raw()]

    assert captured.value.code is TransportErrorCode.READ_TIMEOUT
    assert captured.value.request_bytes_sent == 1
    assert str(captured.value) == "nvidia_transport_error"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    await response.aclose()
    assert stream.close_count == 1


async def test_sanitized_response_maps_close_error_once_without_provider_text() -> None:
    stream = _CloseFailureStream()
    response = SanitizedResponse(httpx2.Response(200, stream=stream), ())

    with pytest.raises(NvidiaTransportError) as captured:
        await response.aclose()

    assert captured.value.code is TransportErrorCode.READ_TIMEOUT
    assert str(captured.value) == "nvidia_transport_error"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    await response.aclose()
    assert stream.close_count == 1
