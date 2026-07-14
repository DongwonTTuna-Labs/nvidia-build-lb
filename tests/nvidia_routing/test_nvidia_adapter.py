"""Origin POST, terminal acquisition, and 202 poll adapter behavior."""

from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Self, override

import anyio
import pytest
from anyio.lowlevel import checkpoint
from pydantic import SecretStr

import nvidia_build_lb.nvidia_adapter as nvidia_adapter_module
import nvidia_build_lb.response_retirement as response_retirement_module
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.nvidia_adapter import NvidiaAdapterDependencies, NvidiaHostedAdapter
from nvidia_build_lb.nvidia_types import NvidiaTransportError
from nvidia_build_lb.outcomes import TransportErrorCode
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.polling import (
    FailureTerminal,
    JsonTerminal,
    ResponseRetirementUnresolvedError,
)
from nvidia_build_lb.request_wire import NvidiaWireRequest
from nvidia_build_lb.transport_common import PinnedTransportDriftError

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _Clock(Clock):
    monotonic_value: float

    def __init__(self) -> None:
        self.monotonic_value = 0.0

    @override
    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.monotonic_value


class _FailingWallClock(_Clock):
    error: RuntimeError

    def __init__(self, error: RuntimeError) -> None:
        super().__init__()
        self.error = error

    @override
    def now(self) -> datetime:
        raise self.error


class _CloseEntryDeadlineClock(_Clock):
    calls: int

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    @override
    def monotonic(self) -> float:
        self.calls += 1
        return 121.0 if self.calls >= 4 else 0.0


class _Sleeper:
    clock: _Clock

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock

    async def sleep(self, seconds: float) -> None:
        self.clock.monotonic_value += seconds


class _Jitter:
    def uniform(self, lower: float, upper: float) -> float:
        del lower
        return upper


class _FailStop:
    calls: int

    def __init__(self) -> None:
        self.calls = 0

    def trigger(self) -> None:
        self.calls += 1


class _Response:
    status_code: int
    raw_headers: tuple[tuple[bytes, bytes], ...]
    chunks: tuple[bytes, ...]
    close_count: int
    iterator_calls: int

    def __init__(
        self,
        status_code: int,
        *,
        headers: tuple[tuple[bytes, bytes], ...] = (),
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.raw_headers = headers
        self.chunks = chunks
        self.close_count = 0
        self.iterator_calls = 0

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.close_count += 1


class _DeadlineResponse(_Response):
    clock: _Clock
    iterator_calls: int

    def __init__(self, clock: _Clock) -> None:
        super().__init__(
            200,
            headers=((b"Content-Type", b"application/json"),),
            chunks=(b'{"id":"late","choices":[],"usage":{}}',),
        )
        self.clock = clock

    @override
    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        self.clock.monotonic_value = 121.0
        yield self.chunks[0]


class _CloseFailureResponse(_Response):
    close_count: int

    @override
    async def aclose(self) -> None:
        if self.close_count:
            return
        self.close_count += 1
        raise NvidiaTransportError(TransportErrorCode.READ_ERROR, 1)


class _AlwaysCloseFailureResponse(_Response):
    close_count: int

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        raise NvidiaTransportError(TransportErrorCode.READ_ERROR, 1)


class _DriftCloseResponse(_Response):
    close_count: int
    error: Exception

    def __init__(
        self,
        error: Exception,
        *,
        status_code: int = 200,
        headers: tuple[tuple[bytes, bytes], ...] | None = None,
    ) -> None:
        super().__init__(
            status_code,
            headers=headers or ((b"Content-Type", b"invalid/provider-type"),),
        )
        self.error = error

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        raise self.error


class _DeadlineCloseFailureResponse(_DeadlineResponse):
    close_count: int

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        raise NvidiaTransportError(TransportErrorCode.READ_ERROR, 1)


class _HangingCloseResponse(_Response):
    close_count: int
    close_finished: anyio.Event
    close_started: anyio.Event
    release: anyio.Event

    def __init__(
        self,
        *,
        status_code: int = 200,
        headers: tuple[tuple[bytes, bytes], ...] | None = None,
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        super().__init__(
            status_code,
            headers=headers or ((b"Content-Type", b"invalid/provider-type"),),
            chunks=chunks,
        )
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


class _BodyTransportFailureHangingCloseResponse(_HangingCloseResponse):
    iterator_calls: int

    def __init__(self) -> None:
        super().__init__(headers=((b"Content-Type", b"application/json"),))

    @override
    def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        return _BodyTransportFailureIterator()


class _DeadlineHangingCloseResponse(_HangingCloseResponse):
    clock: _Clock
    iterator_calls: int

    def __init__(self, clock: _Clock) -> None:
        super().__init__(headers=((b"Content-Type", b"application/json"),))
        self.clock = clock

    @override
    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        self.clock.monotonic_value = 121.0
        yield b'{"id":"late","choices":[],"usage":{}}'


class _CloseDeadlineResponse(_Response):
    close_count: int
    clock: _Clock

    def __init__(self, clock: _Clock) -> None:
        super().__init__(202, headers=((b"NVCF-REQID", b"request-202"),))
        self.clock = clock

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        self.clock.monotonic_value = 60.0


class _BodyTransportFailureResponse(_Response):
    iterator_calls: int

    @override
    def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        return _BodyTransportFailureIterator()


class _HangingBodyResponse(_Response):
    iterator_calls: int
    read_started: anyio.Event

    def __init__(self) -> None:
        super().__init__(
            200,
            headers=((b"Content-Type", b"application/json"),),
        )
        self.read_started = anyio.Event()

    @override
    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        self.read_started.set()
        await anyio.sleep_forever()
        yield b""


class _BodyTransportFailureIterator:
    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        raise NvidiaTransportError(TransportErrorCode.READ_TIMEOUT, 1)


class _Client:
    responses: deque[_Response | NvidiaTransportError]
    requests: list[NvidiaWireRequest]

    def __init__(self, responses: tuple[_Response | NvidiaTransportError, ...]) -> None:
        self.responses = deque(responses)
        self.requests = []

    async def send(self, request: NvidiaWireRequest, *, timeout_seconds: float) -> _Response:
        assert timeout_seconds > 0
        self.requests.append(request)
        response = self.responses.popleft()
        if isinstance(response, NvidiaTransportError):
            raise response
        return response


def _adapter(
    client: _Client,
    clock: _Clock,
    fail_stop: _FailStop | None = None,
) -> NvidiaHostedAdapter:
    return NvidiaHostedAdapter(
        NvidiaAdapterDependencies(
            client=client,
            monotonic_clock=clock,
            wall_clock=clock,
            sleeper=_Sleeper(clock),
            jitter=_Jitter(),
            fail_stop=fail_stop or _FailStop(),
        )
    )


async def test_origin_202_is_closed_unread_then_same_key_poll_returns_json() -> None:
    origin = _Response(202, headers=((b"NVCF-REQID", b"request-202"),), chunks=(b"ignored",))
    body = b'{"id":"r","choices":[],"usage":{}}'
    poll = _Response(
        200,
        headers=((b"Content-Type", b"application/json"),),
        chunks=(body,),
    )
    client = _Client((origin, poll))
    clock = _Clock()
    credential = SecretStr("same-secret")

    result = await _adapter(client, clock).execute(credential=credential, body=b"{}")

    assert isinstance(result, JsonTerminal)
    assert [request.method for request in client.requests] == ["POST", "GET"]
    assert all(request.credential is credential for request in client.requests)
    assert origin.iterator_calls == 0
    assert origin.close_count == 1
    assert poll.close_count == 1


async def test_origin_202_poll_rate_limit_forbids_alternate_key() -> None:
    origin = _Response(202, headers=((b"NVCF-REQID", b"request-202"),))
    poll = _Response(429, headers=((b"Retry-After", b"2"),))
    client = _Client((origin, poll))

    result = await _adapter(client, _Clock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_rate_limited"
    assert result.outcome.alternate_eligible is False
    assert [request.method for request in client.requests] == ["POST", "GET"]
    assert origin.close_count == 1
    assert poll.close_count == 1


async def test_origin_202_close_transport_failure_is_safe_and_forbids_alternate() -> None:
    origin = _CloseFailureResponse(
        202,
        headers=((b"NVCF-REQID", b"request-202"),),
    )

    result = await _adapter(_Client((origin,)), _Clock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.alternate_eligible is False
    assert origin.close_count == 1


@pytest.mark.parametrize("polled", [False, True], ids=("origin_202", "poll_202"))
async def test_202_close_transport_failure_never_recloses_owned_response(
    polled: bool,
) -> None:
    failing = _AlwaysCloseFailureResponse(
        202,
        headers=((b"NVCF-REQID", b"request-202"),),
    )
    responses = (
        (_Response(202, headers=((b"NVCF-REQID", b"request-202"),)), failing)
        if polled
        else (failing,)
    )
    client = _Client(responses)

    result = await _adapter(client, _Clock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.alternate_eligible is False
    assert failing.close_count == 1
    assert len(client.requests) == (2 if polled else 1)


async def test_cancelled_origin_202_close_marks_unresolved_and_fail_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    response = _HangingCloseResponse(
        status_code=202,
        headers=((b"NVCF-REQID", b"request-202"),),
    )
    fail_stop = _FailStop()
    adapter = _adapter(_Client((response,)), _Clock(), fail_stop)
    observed: list[type[BaseException]] = []

    async def execute() -> None:
        try:
            _ = await adapter.execute(credential=SecretStr("same-secret"), body=b"{}")
        except (ResponseRetirementUnresolvedError, anyio.get_cancelled_exc_class()) as error:
            observed.append(type(error))

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(execute)
        await response.close_started.wait()
        tasks.cancel_scope.cancel()

    assert len(observed) == 1
    assert fail_stop.calls == 1
    assert response.close_count == 1
    assert not response.close_finished.is_set()
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_origin_error_body_is_unread_and_retry_after_is_safely_normalized() -> None:
    response = _Response(
        429,
        headers=((b"Retry-After", b"600"),),
        chunks=(b"provider secret error",),
    )
    client = _Client((response,))
    clock = _Clock()

    result = await _adapter(client, clock).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_rate_limited"
    assert result.outcome.alternate_eligible is True
    assert result.retry_after_seconds == 300
    assert response.iterator_calls == 0
    assert response.close_count == 1


@pytest.mark.parametrize(
    ("status_code", "content_type", "expected_code", "retry_after"),
    [
        (418, b"application/json", "upstream_request_rejected", None),
        (451, b"application/problem+json", "upstream_request_rejected", None),
        (429, b"text/plain", "upstream_rate_limited", b"2"),
        (503, b"application/problem+json", "upstream_unavailable", None),
    ],
)
async def test_origin_error_media_type_does_not_replace_status_mapping(
    status_code: int,
    content_type: bytes,
    expected_code: str,
    retry_after: bytes | None,
) -> None:
    headers = [(b"Content-Type", content_type)]
    if retry_after is not None:
        headers.append((b"Retry-After", retry_after))
    response = _Response(
        status_code,
        headers=tuple(headers),
        chunks=(b"provider-controlled error body",),
    )

    result = await _adapter(_Client((response,)), _Clock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == expected_code
    assert result.retry_after_seconds == (2 if retry_after is not None else None)
    assert response.iterator_calls == 0
    assert response.close_count == 1


async def test_origin_connect_failure_is_safe_and_alternate_eligible() -> None:
    client = _Client((NvidiaTransportError(TransportErrorCode.CONNECT_ERROR, 0),))
    clock = _Clock()

    result = await _adapter(client, clock).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_unavailable"
    assert result.outcome.alternate_eligible is True


async def test_header_protocol_terminal_is_not_masked_by_close_failure() -> None:
    response = _CloseFailureResponse(
        200,
        headers=((b"Content-Type", b"invalid/provider-type"),),
    )

    result = await _adapter(_Client((response,)), _Clock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_protocol_error"
    assert response.close_count == 1


@pytest.mark.parametrize("polled", [False, True], ids=("direct", "poll"))
async def test_json_terminal_is_not_reclassified_or_reclosed_after_ordinary_close_failure(
    polled: bool,
) -> None:
    body = b'{"id":"r","choices":[],"usage":{}}'
    terminal = _CloseFailureResponse(
        200,
        headers=((b"Content-Type", b"application/json"),),
        chunks=(body,),
    )
    responses = (
        (_Response(202, headers=((b"NVCF-REQID", b"request-202"),)), terminal)
        if polled
        else (terminal,)
    )

    result = await _adapter(_Client(responses), _Clock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, JsonTerminal)
    assert result.representation.raw == body
    assert result.retirement_unresolved is False
    assert terminal.iterator_calls == 1
    assert terminal.close_count == 1


async def test_json_selected_at_deadline_still_retires_response_once() -> None:
    body = b'{"id":"r","choices":[],"usage":{}}'
    response = _Response(
        200,
        headers=((b"Content-Type", b"application/json"),),
        chunks=(body,),
    )

    result = await _adapter(_Client((response,)), _CloseEntryDeadlineClock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, JsonTerminal)
    assert result.representation.raw == body
    assert result.retirement_unresolved is False
    assert response.close_count == 1


@pytest.mark.parametrize(
    "media_type",
    [
        pytest.param(b"application/json", id="json"),
        pytest.param(b"text/event-stream", id="sse_prime"),
    ],
)
async def test_direct_body_transport_failure_is_safe_and_closes_response(
    media_type: bytes,
) -> None:
    response = _BodyTransportFailureResponse(
        200,
        headers=((b"Content-Type", media_type),),
    )

    result = await _adapter(_Client((response,)), _Clock()).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_timeout"
    assert result.outcome.alternate_eligible is False
    assert response.iterator_calls == 1
    assert response.close_count == 1


async def test_direct_terminal_deadline_maps_safely_and_closes_response_once() -> None:
    clock = _Clock()
    response = _DeadlineResponse(clock)

    result = await _adapter(_Client((response,)), clock).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_timeout"
    assert response.iterator_calls == 1
    assert response.close_count == 1


async def test_direct_deadline_terminal_is_not_masked_by_close_failure() -> None:
    clock = _Clock()
    response = _DeadlineCloseFailureResponse(clock)

    result = await _adapter(_Client((response,)), clock).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_timeout"
    assert response.close_count == 1


async def test_poll_deadline_terminal_is_not_masked_by_close_failure() -> None:
    clock = _Clock()
    origin = _Response(202, headers=((b"NVCF-REQID", b"request-202"),))
    poll = _DeadlineCloseFailureResponse(clock)

    result = await _adapter(_Client((origin, poll)), clock).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "poll_timeout"
    assert origin.close_count == 1
    assert poll.close_count == 1


async def test_selected_protocol_terminal_defers_unresolved_retirement_fail_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    response = _HangingCloseResponse()
    fail_stop = _FailStop()

    result = await _adapter(_Client((response,)), _Clock(), fail_stop).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_protocol_error"
    assert result.retirement_unresolved is True
    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    assert fail_stop.calls == 0
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()
    await checkpoint()


async def test_primary_adapter_error_is_preserved_when_cleanup_is_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    response = _HangingCloseResponse()
    fail_stop = _FailStop()
    primary = RuntimeError("synthetic wall-clock failure")

    with pytest.raises(RuntimeError) as captured:
        _ = await _adapter(
            _Client((response,)),
            _FailingWallClock(primary),
            fail_stop,
        ).execute(
            credential=SecretStr("same-secret"),
            body=b"{}",
        )

    assert captured.value is primary
    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    assert fail_stop.calls == 1
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()
    await checkpoint()


@pytest.mark.parametrize(
    "drift_error",
    [
        pytest.param(PinnedRuntimeDriftError(), id="runtime_drift"),
        pytest.param(PinnedTransportDriftError(), id="transport_drift"),
    ],
)
async def test_selected_protocol_terminal_defers_close_drift_fail_stop(
    drift_error: Exception,
) -> None:
    response = _DriftCloseResponse(drift_error)
    fail_stop = _FailStop()

    result = await _adapter(_Client((response,)), _Clock(), fail_stop).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_protocol_error"
    assert result.retirement_unresolved is True
    assert response.close_count == 1
    assert fail_stop.calls == 0


async def test_rate_limit_terminal_survives_unresolved_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nvidia_adapter_module, "_ORIGIN_TIMEOUT_SECONDS", 0.01)
    response = _HangingCloseResponse(
        status_code=429,
        headers=((b"Retry-After", b"2"),),
    )
    fail_stop = _FailStop()

    result = await _adapter(_Client((response,)), _Clock(), fail_stop).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_rate_limited"
    assert result.retry_after_seconds == 2
    assert result.retirement_unresolved is True
    assert fail_stop.calls == 0
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_direct_read_timeout_terminal_survives_unresolved_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(response_retirement_module, "_RESPONSE_RETIRE_TIMEOUT_SECONDS", 0.01)
    response = _BodyTransportFailureHangingCloseResponse()
    fail_stop = _FailStop()

    result = await _adapter(_Client((response,)), _Clock(), fail_stop).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_timeout"
    assert result.retirement_unresolved is True
    assert fail_stop.calls == 0
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_json_terminal_survives_unresolved_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(nvidia_adapter_module, "_ORIGIN_TIMEOUT_SECONDS", 0.01)
    response = _HangingCloseResponse(
        headers=((b"Content-Type", b"application/json"),),
        chunks=(b'{"id":"r","choices":[],"usage":{}}',),
    )
    fail_stop = _FailStop()

    result = await _adapter(_Client((response,)), _Clock(), fail_stop).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, JsonTerminal)
    assert result.retirement_unresolved is True
    assert fail_stop.calls == 0
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_poll_deadline_terminal_survives_unresolved_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(response_retirement_module, "_RESPONSE_RETIRE_TIMEOUT_SECONDS", 0.01)
    clock = _Clock()
    origin = _Response(202, headers=((b"NVCF-REQID", b"request-202"),))
    poll = _DeadlineHangingCloseResponse(clock)
    fail_stop = _FailStop()

    result = await _adapter(_Client((origin, poll)), clock, fail_stop).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "poll_timeout"
    assert result.retirement_unresolved is True
    assert fail_stop.calls == 0
    poll.release.set()
    with anyio.fail_after(1):
        await poll.close_finished.wait()


@pytest.mark.parametrize(
    "drift_error",
    [
        pytest.param(PinnedRuntimeDriftError(), id="runtime_drift"),
        pytest.param(PinnedTransportDriftError(), id="transport_drift"),
    ],
)
@pytest.mark.parametrize("polled", [False, True], ids=("direct", "poll"))
async def test_error_status_deadline_close_drift_is_deferred_to_routing(
    drift_error: Exception,
    polled: bool,
) -> None:
    response = _DriftCloseResponse(
        drift_error,
        status_code=429,
        headers=((b"Retry-After", b"2"),),
    )
    responses = (
        (_Response(202, headers=((b"NVCF-REQID", b"request-202"),)), response)
        if polled
        else (response,)
    )
    fail_stop = _FailStop()

    result = await _adapter(_Client(responses), _Clock(), fail_stop).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_rate_limited"
    assert result.retirement_unresolved is True
    assert response.close_count == 1
    assert fail_stop.calls == 0


async def test_origin_close_at_poll_deadline_has_one_physical_close() -> None:
    clock = _Clock()
    response = _CloseDeadlineResponse(clock)

    result = await _adapter(_Client((response,)), clock).execute(
        credential=SecretStr("same-secret"),
        body=b"{}",
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "poll_timeout"
    assert response.close_count == 1


async def test_direct_body_cancellation_retires_owned_response_once() -> None:
    response = _HangingBodyResponse()
    client = _Client((response,))
    clock = _Clock()
    scopes: list[anyio.CancelScope] = []

    async def execute() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            _ = await _adapter(client, clock).execute(
                credential=SecretStr("cancel-secret"),
                body=b"{}",
            )

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(execute)
        await response.read_started.wait()
        scopes[0].cancel()

    assert response.close_count == 1
    assert response.iterator_calls == 1
