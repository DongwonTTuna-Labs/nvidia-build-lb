"""Runtime boundary guards and transport-exposed header equivalences."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import override

import pytest
from pydantic import SecretStr

from nvidia_build_lb.headers import MediaType, validate_upstream_headers
from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.pinned_runtime import TransportBoundaryGuard, load_isolated_runtime
from nvidia_build_lb.request_wire import build_nvidia_request
from nvidia_build_lb.transport import SanitizedAsyncClient

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_NOW = datetime(2026, 7, 13, tzinfo=UTC)


class _GuardStream(httpx2.AsyncByteStream):
    close_count: int

    def __init__(self) -> None:
        self.close_count = 0

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"{}"

    @override
    async def aclose(self) -> None:
        self.close_count += 1


class _GuardTransport(httpx2.AsyncBaseTransport):
    calls: int
    close_count: int
    stream: _GuardStream

    def __init__(self) -> None:
        self.calls = 0
        self.close_count = 0
        self.stream = _GuardStream()

    @override
    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        assert set(request.extensions) == {"timeout"}
        return httpx2.Response(
            200,
            headers=(
                (b"Content-Type", b" application/json "),
                (b"Content-Length", b" 2\t"),
                (b"X-Provider-Private", b"never-expose"),
            ),
            stream=self.stream,
        )

    @override
    async def aclose(self) -> None:
        self.close_count += 1


async def test_transport_guard_sanitizes_response_and_closes_resources() -> None:
    runtime = load_isolated_runtime()
    snapshot = TransportBoundaryGuard(runtime.module_names).snapshot()
    transport = _GuardTransport()
    client = SanitizedAsyncClient.from_transport_for_test(transport)
    request = build_nvidia_request(
        credential=SecretStr("synthetic-secret"),
        body=b"{}",
    )

    response = await client.send(request, timeout_seconds=10.0)
    validated = validate_upstream_headers(
        status_code=response.status_code,
        raw_headers=response.raw_headers,
        now=_NOW,
    )

    assert snapshot.forbidden_module_count == 0
    assert transport.calls == 1
    assert response.raw_headers == (
        (b"Content-Type", b" application/json "),
        (b"Content-Length", b" 2\t"),
    )
    assert validated.media_type is MediaType.JSON
    assert validated.content_length == 2
    assert validated.application_headers == (
        (b"Content-Type", b"application/json"),
        (b"Content-Length", b"2"),
    )
    await response.aclose()
    await client.aclose()
    assert transport.stream.close_count == 1
    assert transport.close_count == 1
