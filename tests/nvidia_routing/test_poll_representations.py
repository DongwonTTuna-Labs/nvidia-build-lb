"""Same-key 202 polling through strict terminal representations."""

from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import override

import anyio
import pytest
from anyio.lowlevel import checkpoint
from pydantic import SecretStr

import nvidia_build_lb.response_retirement as response_retirement_module
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.outcomes import RoutingTransition
from nvidia_build_lb.polling import (
    FailureTerminal,
    JsonTerminal,
    PollDependencies,
    PollingDeadline,
    ResponseRetirementUnresolvedError,
    StreamTerminal,
    poll_nvcf_result,
)
from nvidia_build_lb.request_wire import NvidiaWireRequest

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _ManualClock(Clock):
    wall: datetime
    monotonic_value: float

    def __init__(self) -> None:
        self.wall = datetime(2026, 1, 1, tzinfo=UTC)
        self.monotonic_value = 0.0

    @override
    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value


class _Sleeper:
    clock: _ManualClock
    observed: list[float]

    def __init__(self, clock: _ManualClock) -> None:
        self.clock = clock
        self.observed = []

    async def sleep(self, seconds: float) -> None:
        self.observed.append(seconds)
        self.clock.monotonic_value += seconds


class _TimelineSleeper(_Sleeper):
    timeline: list[str]

    def __init__(self, clock: _ManualClock, timeline: list[str]) -> None:
        super().__init__(clock)
        self.timeline = timeline

    @override
    async def sleep(self, seconds: float) -> None:
        self.timeline.append(f"sleep:{seconds}")
        await super().sleep(seconds)


class _UpperJitter:
    def uniform(self, lower: float, upper: float) -> float:
        assert lower == upper / 2
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


class _CancellationResponse(_Response):
    close_count: int
    close_finished: int
    close_started: int
    iterator_calls: int
    iterator_waiting: anyio.Event

    def __init__(self) -> None:
        super().__init__(
            200,
            headers=((b"Content-Type", b"text/event-stream"),),
            chunks=(b"data: one\n\n", b"data: [DONE]\n\n"),
        )
        self.close_started = 0
        self.close_finished = 0
        self.iterator_waiting = anyio.Event()

    @override
    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        yield b"data: one\n\n"
        self.iterator_waiting.set()
        await anyio.sleep_forever()

    @override
    async def aclose(self) -> None:
        self.close_started += 1
        await checkpoint()
        self.close_finished += 1
        self.close_count += 1


class _UnresolvedCloseResponse(_Response):
    close_count: int
    close_finished: anyio.Event
    close_started: anyio.Event
    release: anyio.Event

    def __init__(self) -> None:
        super().__init__(
            200,
            headers=((b"Content-Type", b"text/event-stream"),),
            chunks=(b"data: one\n\n", b"data: [DONE]\n\n"),
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


class _Client:
    responses: deque[_Response]
    requests: list[NvidiaWireRequest]
    timeout_budgets: list[float]

    def __init__(self, responses: tuple[_Response, ...]) -> None:
        self.responses = deque(responses)
        self.requests = []
        self.timeout_budgets = []

    async def send(self, request: NvidiaWireRequest, *, timeout_seconds: float) -> _Response:
        self.requests.append(request)
        self.timeout_budgets.append(timeout_seconds)
        return self.responses.popleft()


class _TimelineClient(_Client):
    timeline: list[str]

    def __init__(self, responses: tuple[_Response, ...], timeline: list[str]) -> None:
        super().__init__(responses)
        self.timeline = timeline

    @override
    async def send(self, request: NvidiaWireRequest, *, timeout_seconds: float) -> _Response:
        self.timeline.append("send")
        return await super().send(request, timeout_seconds=timeout_seconds)


class _DeadlineOnSendClient(_Client):
    clock: _ManualClock

    def __init__(self, response: _Response, clock: _ManualClock) -> None:
        super().__init__((response,))
        self.clock = clock

    @override
    async def send(self, request: NvidiaWireRequest, *, timeout_seconds: float) -> _Response:
        response = await super().send(request, timeout_seconds=timeout_seconds)
        self.clock.monotonic_value = 60.0
        return response


def _dependencies(
    client: _Client,
    clock: _ManualClock,
    sleeper: _Sleeper,
    fail_stop: _FailStop | None = None,
) -> PollDependencies:
    return PollDependencies(
        client=client,
        monotonic_clock=clock,
        wall_clock=clock,
        sleeper=sleeper,
        jitter=_UpperJitter(),
        fail_stop=fail_stop or _FailStop(),
    )


async def test_first_poll_waits_equal_jitter_before_send_and_json_is_bounded() -> None:
    pending = _Response(202)
    body = b'{"id":"r","choices":[],"usage":{}}'
    terminal = _Response(
        200,
        headers=(
            (b"Content-Type", b"application/json"),
            (b"Content-Length", str(len(body)).encode("ascii")),
        ),
        chunks=(body,),
    )
    timeline: list[str] = []
    client = _TimelineClient((pending, terminal), timeline)
    clock = _ManualClock()
    sleeper = _TimelineSleeper(clock, timeline)
    credential = SecretStr("same-secret")

    result = await poll_nvcf_result(
        dependencies=_dependencies(client, clock, sleeper),
        credential=credential,
        request_id="request-202",
        deadline=PollingDeadline(clock=clock, expires_at=60.0),
    )

    assert isinstance(result, JsonTerminal)
    assert result.representation.value["id"] == "r"
    assert sleeper.observed == [0.25, 0.5]
    assert timeline == ["sleep:0.25", "send", "sleep:0.5", "send"]
    assert [request.path for request in client.requests] == [
        "/v2/nvcf/pexec/status/request-202",
        "/v2/nvcf/pexec/status/request-202",
    ]
    assert all(request.credential is credential for request in client.requests)
    assert pending.iterator_calls == 0
    assert pending.close_count == 1
    assert terminal.iterator_calls == 1
    assert terminal.close_count == 1


async def test_deadline_equality_after_send_closes_the_newly_owned_response() -> None:
    clock = _ManualClock()
    response = _Response(200, headers=((b"Content-Type", b"application/json"),))
    client = _DeadlineOnSendClient(response, clock)

    result = await poll_nvcf_result(
        dependencies=_dependencies(client, clock, _Sleeper(clock)),
        credential=SecretStr("same-secret"),
        request_id="request-202",
        deadline=PollingDeadline(clock=clock, expires_at=60.0),
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "poll_timeout"
    assert len(client.requests) == 1
    assert response.close_count == 1


async def test_poll_sse_is_primed_inside_deadline_then_continues_same_iterator() -> None:
    terminal = _Response(
        200,
        headers=((b"Content-Type", b"text/event-stream"),),
        chunks=(b"data: one\n\n", b"data: [DONE]\n\nignored"),
    )
    client = _Client((terminal,))
    clock = _ManualClock()
    sleeper = _Sleeper(clock)

    result = await poll_nvcf_result(
        dependencies=_dependencies(client, clock, sleeper),
        credential=SecretStr("same-secret"),
        request_id="request-202",
        deadline=PollingDeadline(clock=clock, expires_at=60.0),
    )

    assert isinstance(result, StreamTerminal)
    observed = [frame async for frame in result.stream.frames()]
    assert [frame.data for frame in observed] == [b"one", b"[DONE]"]
    assert terminal.iterator_calls == 1
    assert terminal.close_count == 1


async def test_changed_poll_request_id_closes_unread_and_fails_protocol() -> None:
    changed = _Response(202, headers=((b"NVCF-REQID", b"different"),))
    client = _Client((changed,))
    clock = _ManualClock()
    sleeper = _Sleeper(clock)

    result = await poll_nvcf_result(
        dependencies=_dependencies(client, clock, sleeper),
        credential=SecretStr("same-secret"),
        request_id="request-202",
        deadline=PollingDeadline(clock=clock, expires_at=60.0),
    )

    assert isinstance(result, FailureTerminal)
    assert result.outcome.code == "upstream_protocol_error"
    assert result.outcome.transition is RoutingTransition.DEGRADE_HEALTH_ONLY
    assert changed.iterator_calls == 0
    assert changed.close_count == 1


async def test_stream_cancellation_completes_physical_close_before_losing_ownership() -> None:
    response = _CancellationResponse()
    client = _Client((response,))
    clock = _ManualClock()
    result = await poll_nvcf_result(
        dependencies=_dependencies(client, clock, _Sleeper(clock)),
        credential=SecretStr("same-secret"),
        request_id="request-202",
        deadline=PollingDeadline(clock=clock, expires_at=60.0),
    )
    assert isinstance(result, StreamTerminal)

    async def consume() -> None:
        _ = [frame async for frame in result.stream.frames()]

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(consume)
        await response.iterator_waiting.wait()
        tasks.cancel_scope.cancel()

    assert response.close_started == 1
    assert response.close_finished == 1
    assert response.close_count == 1
    await result.stream.aclose()
    assert response.close_count == 1


async def test_concurrent_stream_close_has_one_physical_owner() -> None:
    response = _CancellationResponse()
    client = _Client((response,))
    clock = _ManualClock()
    result = await poll_nvcf_result(
        dependencies=_dependencies(client, clock, _Sleeper(clock)),
        credential=SecretStr("same-secret"),
        request_id="request-202",
        deadline=PollingDeadline(clock=clock, expires_at=60.0),
    )
    assert isinstance(result, StreamTerminal)

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(result.stream.aclose)
        _ = tasks.start_soon(result.stream.aclose)

    assert response.close_started == 1
    assert response.close_finished == 1
    assert response.close_count == 1


async def test_handed_off_stream_unresolved_retirement_triggers_fail_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    response = _UnresolvedCloseResponse()
    fail_stop = _FailStop()
    clock = _ManualClock()
    result = await poll_nvcf_result(
        dependencies=_dependencies(_Client((response,)), clock, _Sleeper(clock), fail_stop),
        credential=SecretStr("same-secret"),
        request_id="request-202",
        deadline=PollingDeadline(clock=clock, expires_at=60.0),
    )
    assert isinstance(result, StreamTerminal)

    with pytest.raises(ResponseRetirementUnresolvedError):
        await result.stream.aclose()

    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    assert fail_stop.calls == 1
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()
