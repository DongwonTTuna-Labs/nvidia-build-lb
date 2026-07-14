"""Pinned H1 transport conversion, ownership, and fail-closed behavior."""

import sys
from collections.abc import AsyncIterator, Iterator
from types import ModuleType
from typing import override

import anyio
import pytest
from anyio.lowlevel import checkpoint

import nvidia_build_lb.response_retirement as response_retirement_module
import nvidia_build_lb.transport_h1 as transport_h1_module
from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError, load_isolated_runtime
from nvidia_build_lb.response_retirement import (
    ResponseRetirementUnresolvedError,
    retire_response,
)
from nvidia_build_lb.transport import (
    CoreAsyncStream,
    CoreRequest,
    CoreResponse,
    PinnedH1Transport,
    PinnedTransportDriftError,
    load_transport_core,
)

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _CoreStream:
    close_count: int

    def __init__(self) -> None:
        self.close_count = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.close_count += 1


class _FailingCloseCoreStream(_CoreStream):
    close_count: int

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        raise _ExplodingError


class _HangingCloseCoreStream(_CoreStream):
    close_count: int
    close_finished: anyio.Event
    close_started: anyio.Event
    release: anyio.Event

    def __init__(self) -> None:
        super().__init__()
        self.close_started = anyio.Event()
        self.close_finished = anyio.Event()
        self.release = anyio.Event()

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        self.close_started.set()
        with anyio.CancelScope(shield=True):
            await self.release.wait()
        self.close_finished.set()


class _CoreResponse:
    status: int
    headers: list[tuple[bytes, bytes]]
    stream: CoreAsyncStream
    extensions: dict[str, object]

    def __init__(self, stream: CoreAsyncStream) -> None:
        self.status = 200
        self.headers = [(b"content-type", b"text/event-stream")]
        self.stream = stream
        self.extensions = {
            "http_version": b"HTTP/1.1",
            "reason_phrase": _ExplodingText(),
        }


class _Pool:
    response: CoreResponse
    requests: list[CoreRequest]
    close_count: int

    def __init__(self, response: CoreResponse) -> None:
        self.response = response
        self.requests = []
        self.close_count = 0

    async def handle_async_request(self, request: CoreRequest) -> CoreResponse:
        self.requests.append(request)
        return self.response

    async def aclose(self) -> None:
        self.close_count += 1


class _HangingClosePool(_Pool):
    close_count: int
    close_finished: anyio.Event
    close_started: anyio.Event
    release: anyio.Event

    def __init__(self, response: CoreResponse) -> None:
        super().__init__(response)
        self.close_started = anyio.Event()
        self.close_finished = anyio.Event()
        self.release = anyio.Event()

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        self.close_started.set()
        with anyio.CancelScope(shield=True):
            await self.release.wait()
        self.close_finished.set()


class _ExplodingText:
    @override
    def __str__(self) -> str:
        raise AssertionError

    @override
    def __repr__(self) -> str:
        raise AssertionError


class _ExplodingHeaders(list[tuple[bytes, bytes]]):
    @override
    def __iter__(self) -> Iterator[tuple[bytes, bytes]]:
        raise _ExplodingError


class _ConstructionFailureResponse:
    status: int = 200
    headers: list[tuple[bytes, bytes]]
    extensions: dict[str, object]
    stream: CoreAsyncStream

    def __init__(self, stream: CoreAsyncStream) -> None:
        self.headers = _ExplodingHeaders()
        self.extensions = {}
        self.stream = stream


class _HttpxResponseAdapter:
    status_code: int
    raw_headers: tuple[tuple[bytes, bytes], ...]
    response: httpx2.Response

    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.status_code = response.status_code
        self.raw_headers = tuple(response.headers.raw)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_raw():
            yield chunk

    def aiter_raw(self) -> AsyncIterator[bytes]:
        return self.__aiter__()

    async def aclose(self) -> None:
        await self.response.aclose()


def _guarded_request() -> httpx2.Request:
    return httpx2.Request(
        "GET",
        "https://integrate.api.nvidia.com/",
        extensions={"timeout": {"connect": 5.0, "pool": 2.0, "read": 120.0, "write": 15.0}},
    )


async def test_transport_converts_once_and_closes_stream_before_pool() -> None:
    raw_stream = _CoreStream()
    pool = _Pool(_CoreResponse(raw_stream))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    request = httpx2.Request(
        "POST",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        headers=((b"content-type", b"application/json"),),
        content=b"{}",
        extensions={"timeout": {"connect": 5.0, "pool": 2.0, "read": 120.0, "write": 15.0}},
    )

    response = await transport.handle_async_request(request)
    assert isinstance(response.stream, httpx2.AsyncByteStream)
    chunks = [chunk async for chunk in response.stream]
    await response.aclose()
    await transport.aclose()

    assert chunks == [b"data: [DONE]\n\n"]
    assert len(pool.requests) == 1
    core_request = pool.requests[0]
    assert core_request.method == b"POST"
    assert core_request.url.scheme == b"https"
    assert core_request.url.host == b"integrate.api.nvidia.com"
    assert core_request.url.target == b"/v1/chat/completions"
    assert core_request.headers == [
        (b"Host", b"integrate.api.nvidia.com"),
        (b"content-type", b"application/json"),
        (b"Content-Length", b"2"),
    ]
    assert raw_stream.close_count == 1
    assert pool.close_count == 1
    assert transport.configuration.http1 is True
    assert transport.configuration.http2 is False
    assert transport.configuration.retries == 0


async def test_transport_rejects_trace_extension_before_pool_call() -> None:
    pool = _Pool(_CoreResponse(_CoreStream()))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    request = httpx2.Request(
        "GET",
        "https://integrate.api.nvidia.com/v1/chat/completions",
        extensions={"trace": object()},
    )

    with pytest.raises(PinnedTransportDriftError):
        _ = await transport.handle_async_request(request)

    assert pool.requests == []
    await transport.aclose()


async def test_unknown_pool_exception_is_mapped_without_text_or_repr_access() -> None:
    class _FailingPool(_Pool):
        @override
        async def handle_async_request(self, request: CoreRequest) -> CoreResponse:
            self.requests.append(request)
            raise _ExplodingError

    pool = _FailingPool(_CoreResponse(_CoreStream()))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    request = httpx2.Request("GET", "https://integrate.api.nvidia.com/")

    with pytest.raises(PinnedTransportDriftError) as captured:
        _ = await transport.handle_async_request(request)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    await transport.aclose()


async def test_response_construction_failure_retires_raw_stream_once() -> None:
    raw_stream = _CoreStream()
    pool = _Pool(_ConstructionFailureResponse(raw_stream))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))

    with pytest.raises(PinnedTransportDriftError) as captured:
        _ = await transport.handle_async_request(_guarded_request())

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_stream.close_count == 1
    await transport.aclose()
    assert pool.close_count == 1


@pytest.mark.parametrize(
    ("present", "http_version"),
    [
        (True, b"HTTP/2"),
        (True, b"HTTP/1.0"),
        (True, None),
        (True, "HTTP/1.1"),
        (False, None),
    ],
    ids=("http2", "http10", "none", "wrong_type", "missing"),
)
async def test_transport_rejects_nonexact_h1_response_metadata_and_retires_once(
    present: bool,
    http_version: object,
) -> None:
    raw_stream = _CoreStream()
    core_response = _CoreResponse(raw_stream)
    if present:
        core_response.extensions["http_version"] = http_version
    else:
        del core_response.extensions["http_version"]
    pool = _Pool(core_response)
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))

    with pytest.raises(PinnedTransportDriftError):
        _ = await transport.handle_async_request(_guarded_request())

    assert raw_stream.close_count == 1
    await transport.aclose()
    assert raw_stream.close_count == 1
    assert pool.close_count == 1


async def test_transport_close_retires_pool_after_stream_close_failure() -> None:
    raw_stream = _FailingCloseCoreStream()
    pool = _Pool(_CoreResponse(raw_stream))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    _ = await transport.handle_async_request(_guarded_request())

    with pytest.raises(PinnedTransportDriftError) as captured:
        await transport.aclose()

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_stream.close_count == 1
    assert pool.close_count == 1
    await transport.aclose()
    assert pool.close_count == 1


async def test_transport_stream_close_inner_shield_is_hard_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_h1_module, "_TRANSPORT_CLOSE_TIMEOUT_SECONDS", 0.01)
    raw_stream = _HangingCloseCoreStream()
    pool = _Pool(_CoreResponse(raw_stream))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    _ = await transport.handle_async_request(_guarded_request())

    with anyio.fail_after(0.2):
        with pytest.raises(PinnedTransportDriftError):
            await transport.aclose()

    assert raw_stream.close_started.is_set()
    assert not raw_stream.close_finished.is_set()
    assert pool.close_count == 1
    raw_stream.release.set()
    with anyio.fail_after(1):
        await raw_stream.close_finished.wait()


async def test_detached_response_close_and_transport_shutdown_share_one_raw_close_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(transport_h1_module, "_TRANSPORT_CLOSE_TIMEOUT_SECONDS", 0.01)
    raw_stream = _HangingCloseCoreStream()
    pool = _Pool(_CoreResponse(raw_stream))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    response = await transport.handle_async_request(_guarded_request())

    with pytest.raises(ResponseRetirementUnresolvedError):
        await retire_response(_HttpxResponseAdapter(response))
    with pytest.raises(PinnedTransportDriftError):
        await transport.aclose()

    assert raw_stream.close_started.is_set()
    assert not raw_stream.close_finished.is_set()
    assert raw_stream.close_count == 1
    assert pool.close_count == 1
    raw_stream.release.set()
    with anyio.fail_after(1):
        await raw_stream.close_finished.wait()
    await checkpoint()
    assert raw_stream.close_count == 1


async def test_transport_pool_close_inner_shield_is_hard_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transport_h1_module, "_TRANSPORT_CLOSE_TIMEOUT_SECONDS", 0.01)
    pool = _HangingClosePool(_CoreResponse(_CoreStream()))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))

    with anyio.fail_after(0.2):
        with pytest.raises(PinnedTransportDriftError):
            await transport.aclose()

    assert pool.close_started.is_set()
    assert not pool.close_finished.is_set()
    pool.release.set()
    with anyio.fail_after(1):
        await pool.close_finished.wait()


async def test_stream_iteration_guard_detects_module_drift_before_next_chunk() -> None:
    raw_stream = _CoreStream()
    pool = _Pool(_CoreResponse(raw_stream))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    response = await transport.handle_async_request(_guarded_request())
    stream = response.stream
    assert isinstance(stream, httpx2.AsyncByteStream)
    iterator = stream.__aiter__()
    module_name = load_isolated_runtime().module_names[-1]
    original = sys.modules[module_name]
    sys.modules[module_name] = ModuleType(module_name)
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            _ = await anext(iterator)
    finally:
        sys.modules[module_name] = original

    await stream.aclose()
    await transport.aclose()
    assert raw_stream.close_count == 1


async def test_stream_close_guard_drift_does_not_consume_close_ownership() -> None:
    raw_stream = _CoreStream()
    pool = _Pool(_CoreResponse(raw_stream))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    response = await transport.handle_async_request(_guarded_request())
    stream = response.stream
    assert isinstance(stream, httpx2.AsyncByteStream)
    module_name = load_isolated_runtime().module_names[-1]
    original = sys.modules[module_name]
    sys.modules[module_name] = ModuleType(module_name)
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            await stream.aclose()
    finally:
        sys.modules[module_name] = original

    await stream.aclose()
    await transport.aclose()
    assert raw_stream.close_count == 1


async def test_client_close_guard_drift_does_not_consume_pool_close_ownership() -> None:
    pool = _Pool(_CoreResponse(_CoreStream()))
    transport = PinnedH1Transport(core=load_transport_core(pool_override=pool))
    module_name = load_isolated_runtime().module_names[-1]
    original = sys.modules[module_name]
    sys.modules[module_name] = ModuleType(module_name)
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            await transport.aclose()
    finally:
        sys.modules[module_name] = original

    await transport.aclose()
    assert pool.close_count == 1


class _ExplodingError(Exception):
    @override
    def __str__(self) -> str:
        raise AssertionError

    @override
    def __repr__(self) -> str:
        raise AssertionError
