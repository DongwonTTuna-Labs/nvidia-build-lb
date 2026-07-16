"""Response-start, converted JSON, and live SSE terminal send ordering."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Self, override
from uuid import uuid4

import anyio
import pytest
from pydantic import SecretStr

import nvidia_build_lb.response_retirement as response_retirement_module
from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.asgi_response import sse_response_start
from nvidia_build_lb.attempt_fail_stop import AttemptFailStop, LifecycleAttemptFailStop
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptIdentity,
    AttemptLease,
    TerminalCommitted,
)
from nvidia_build_lb.headers import MediaType, ValidatedUpstreamHeaders
from nvidia_build_lb.nvidia_types import NvidiaTransportError
from nvidia_build_lb.outcomes import TransportErrorCode
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.polling import (
    JsonTerminal,
    NvidiaSSEStream,
    ResponseRetirementUnresolvedError,
    StreamTerminal,
)
from nvidia_build_lb.polling_types import PrimedSSEState
from nvidia_build_lb.representations import (
    JsonRepresentation,
    RepresentationProtocolError,
    json_to_sse_frames,
)
from nvidia_build_lb.routing import RoutedJson, RoutedStream
from nvidia_build_lb.sse import SSEFrame, SSEFrameParser
from nvidia_build_lb.stream_reads import next_frame_or_disconnect
from nvidia_build_lb.streaming import ChatStreamResponder
from nvidia_build_lb.streaming_control import (
    DownstreamDisconnectedError,
    DownstreamTerminalStopError,
    StreamContext,
    StreamState,
    send_or_stop,
)
from nvidia_build_lb.terminal import (
    ChatSupervisor,
    ChatSupervisorDependencies,
    TerminalKind,
    TerminalProposal,
)
from nvidia_build_lb.transport_common import PinnedTransportDriftError

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_NOW = datetime(2026, 7, 13, tzinfo=UTC)


class _Clock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 2.0


class _FailStop:
    calls: int

    def __init__(self) -> None:
        self.calls = 0

    def trigger(self) -> None:
        self.calls += 1


class _Readiness:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def set_ready(self, ready: bool) -> None:
        self.events.append(f"ready:{str(ready).lower()}")


class _Attempts:
    events: list[str]
    commands: list[AttemptFinalizeCommand]

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.commands = []

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        self.events.append("terminal_persist")
        self.commands.append(command)
        return TerminalCommitted(command.identity)


class _FailingAttempts(_Attempts):
    error: RuntimeError

    def __init__(self, events: list[str], error: RuntimeError) -> None:
        super().__init__(events)
        self.error = error

    @override
    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        del command
        self.events.append("terminal_persist")
        raise self.error


class _DoneRaceSupervisor(ChatSupervisor):
    done_precheck_returned_false: anyio.Event
    allow_done_publish: anyio.Event

    def __init__(self, dependencies: ChatSupervisorDependencies) -> None:
        super().__init__(dependencies)
        self.done_precheck_returned_false = anyio.Event()
        self.allow_done_publish = anyio.Event()

    @override
    async def has_pending(self, *, generation: int) -> bool:
        pending = await super().has_pending(generation=generation)
        if generation == 1 and not pending:
            self.done_precheck_returned_false.set()
            await self.allow_done_publish.wait()
        return pending


class _Response:
    status_code: int = 200
    raw_headers: tuple[tuple[bytes, bytes], ...] = ()
    chunks: tuple[bytes, ...]
    close_count: int

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.close_count = 0

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.close_count += 1


class _HangingResponse(_Response):
    iterator_waiting: anyio.Event

    def __init__(self) -> None:
        super().__init__(())
        self.iterator_waiting = anyio.Event()

    @override
    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_waiting.set()
        await anyio.sleep_forever()
        yield b""


class _TransportFailureResponse(_Response):
    @override
    def aiter_raw(self) -> AsyncIterator[bytes]:
        return _TransportFailureIterator()


class _CloseFailureResponse(_Response):
    close_count: int
    error: RuntimeError

    def __init__(self, chunks: tuple[bytes, ...], error: RuntimeError) -> None:
        super().__init__(chunks)
        self.error = error

    @override
    async def aclose(self) -> None:
        self.close_count += 1
        raise self.error


class _UnresolvedCloseResponse(_Response):
    close_count: int
    close_finished: anyio.Event
    close_started: anyio.Event
    release: anyio.Event

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        super().__init__(chunks)
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


class _PrimaryFailureUnresolvedCloseResponse(_UnresolvedCloseResponse):
    error: Exception

    def __init__(self, error: Exception) -> None:
        super().__init__(())
        self.error = error

    @override
    def aiter_raw(self) -> AsyncIterator[bytes]:
        return _PrimaryFailureIterator(self.error)


class _PrimaryFailureResponse(_Response):
    error: Exception

    def __init__(self, error: Exception) -> None:
        super().__init__(())
        self.error = error

    @override
    def aiter_raw(self) -> AsyncIterator[bytes]:
        return _PrimaryFailureIterator(self.error)


class _TransportFailureIterator:
    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        raise NvidiaTransportError(TransportErrorCode.READ_TIMEOUT, 1)


class _PrimaryFailureIterator:
    error: Exception

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> bytes:
        raise self.error


class _SequencedDisconnect:
    checks: int

    def __init__(self) -> None:
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks > 1

    async def wait(self) -> None:
        return


class _CountingFrameIterator:
    calls: int

    def __init__(self) -> None:
        self.calls = 0

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> SSEFrame:
        self.calls += 1
        return SSEFrame(raw=b"data: x\n\n", data=b"x", done=False)


def _lease() -> AttemptLease:
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)
    return AttemptLease(identity, uuid4(), SecretStr("synthetic"))


def _stream_terminal(
    response: _Response | None = None,
    fail_stop: AttemptFailStop | None = None,
) -> StreamTerminal:
    first_chunk = b'data: {"choices":[]}\n\n'
    response = response or _Response((b"data: [DONE]\n\n",))
    parser = SSEFrameParser(max_frame_bytes=1024)
    initial = parser.feed(first_chunk)
    stream = NvidiaSSEStream(
        response=response,
        iterator=response.aiter_raw().__aiter__(),
        state=PrimedSSEState(parser, initial, None),
        fail_stop=fail_stop or _FailStop(),
    )
    return StreamTerminal(
        stream=stream,
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.SSE,
            content_length=None,
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"text/event-stream"),),
        ),
    )


def _hanging_stream_terminal() -> tuple[StreamTerminal, _HangingResponse]:
    response = _HangingResponse()
    parser = SSEFrameParser(max_frame_bytes=1024)
    initial = parser.feed(b'data: {"choices":[]}\n\n')
    stream = NvidiaSSEStream(
        response=response,
        iterator=response.aiter_raw().__aiter__(),
        state=PrimedSSEState(parser, initial, None),
        fail_stop=_FailStop(),
    )
    terminal = StreamTerminal(
        stream=stream,
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.SSE,
            content_length=None,
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"text/event-stream"),),
        ),
    )
    return terminal, response


async def _receive_forever() -> dict[str, object]:
    await anyio.sleep_forever()
    return {"type": "http.disconnect"}


async def test_live_sse_persists_success_before_done_terminal_send() -> None:
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _Response((b"data: [DONE]\n\n",))
    routed = RoutedStream(_stream_terminal(response), _lease(), 1, 1.0)
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.body" and message.get("more_body") is False:
            events.append("terminal_send")
        sent.append(message)

    status = await ChatStreamResponder(supervisor).run_stream_status(
        routed=routed,
        receive=_receive_forever,
        send=send,
    )

    assert status is LastStatusClass.SUCCESS
    assert events == ["terminal_persist", "terminal_send"]
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]
    assert sent[1]["more_body"] is True
    assert sent[2]["body"] == b"data: [DONE]\n\n"
    assert sent[2]["more_body"] is False
    assert response.close_count == 1


async def test_done_send_exception_closes_then_reraises_same_instance() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _Response((b"data: [DONE]\n\n",))
    error = RuntimeError("synthetic terminal send failure")

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.body" and message.get("more_body") is False:
            raise error

    with pytest.raises(RuntimeError) as raised:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
            receive=_receive_forever,
            send=send,
        )

    assert raised.value is error
    assert response.close_count == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.SUCCESS


async def test_done_send_exception_is_not_replaced_by_secondary_close_failure() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    send_error = RuntimeError("synthetic primary send failure")
    close_error = RuntimeError("synthetic secondary close failure")
    response = _CloseFailureResponse((b"data: [DONE]\n\n",), close_error)

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.body" and message.get("more_body") is False:
            raise send_error

    with pytest.raises(RuntimeError) as raised:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
            receive=_receive_forever,
            send=send,
        )

    assert raised.value is send_error
    assert response.close_count == 1


async def test_durable_done_success_is_not_replaced_by_ordinary_close_failure() -> None:
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    close_error = RuntimeError("synthetic post-terminal close failure")
    response = _CloseFailureResponse((b"data: [DONE]\n\n",), close_error)
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.body" and message.get("more_body") is False:
            events.append("terminal_send")
        sent.append(message)

    await ChatStreamResponder(supervisor).run_stream(
        routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
        receive=_receive_forever,
        send=send,
    )

    assert events == ["terminal_persist", "terminal_send"]
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.SUCCESS
    assert sent[-1]["more_body"] is False
    assert response.close_count == 1


async def test_competing_disconnect_at_done_is_not_replaced_by_ordinary_close_failure() -> None:
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = _DoneRaceSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    close_error = RuntimeError("synthetic post-stop close failure")
    response = _CloseFailureResponse((b"data: [DONE]\n\n",), close_error)
    raised: list[Exception] = []
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def run_response() -> None:
        try:
            await ChatStreamResponder(supervisor).run_stream(
                routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
                receive=_receive_forever,
                send=send,
            )
        except Exception as error:  # noqa: BLE001 - the assertion records any leaked close error.
            raised.append(error)

    with anyio.fail_after(1):
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(run_response)
            await supervisor.done_precheck_returned_false.wait()
            _ = await supervisor.coordinate(
                TerminalProposal.for_kind(TerminalKind.EXPLICIT_DISCONNECT, sequence=1),
                generation=1,
            )
            supervisor.allow_done_publish.set()

    assert raised == []
    assert events == ["terminal_persist"]
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED
    assert response.close_count == 1
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[1]["more_body"] is True
    assert all(message.get("body") != b"data: [DONE]\n\n" for message in sent)


async def test_real_fail_stop_after_done_success_propagates_unresolved_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _UnresolvedCloseResponse((b"data: [DONE]\n\n",))
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(_Readiness(events), root_scope)
    with pytest.raises(ResponseRetirementUnresolvedError), root_scope:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(
                _stream_terminal(response, fail_stop),
                _lease(),
                1,
                1.0,
            ),
            receive=_receive_forever,
            send=send,
        )

    assert events[:2] == ["terminal_persist", "ready:false"]
    assert root_scope.cancel_called is True
    assert fail_stop.triggered is True
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.SUCCESS
    assert sent[-1]["more_body"] is False
    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_root_cancellation_during_send_propagates_unresolved_retirement_fail_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _UnresolvedCloseResponse(())
    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(_Readiness(events), root_scope)

    async def send(_message: dict[str, object]) -> None:
        root_scope.cancel()
        await anyio.sleep_forever()

    with pytest.raises(ResponseRetirementUnresolvedError), root_scope:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(_stream_terminal(response, fail_stop), _lease(), 1, 1.0),
            receive=_receive_forever,
            send=send,
        )

    assert events == ["terminal_persist", "ready:false"]
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED
    assert root_scope.cancel_called is True
    assert fail_stop.triggered is True
    assert response.close_count == 1
    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_primary_send_error_is_preserved_when_close_is_unresolved_and_fail_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    primary = RuntimeError("synthetic primary send failure")
    response = _UnresolvedCloseResponse((b"data: [DONE]\n\n",))
    fail_stop = _FailStop()

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.body" and message.get("more_body") is False:
            raise primary

    with pytest.raises(RuntimeError) as captured:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(
                _stream_terminal(response, fail_stop),
                _lease(),
                1,
                1.0,
            ),
            receive=_receive_forever,
            send=send,
        )

    assert captured.value is primary
    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    assert fail_stop.calls == 1
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_disconnect_persistence_failure_is_not_replaced_by_close_failure() -> None:
    persistence_error = RuntimeError("synthetic disconnect persistence failure")
    close_error = RuntimeError("synthetic secondary close failure")
    attempts = _FailingAttempts([], persistence_error)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _CloseFailureResponse((b"data: [DONE]\n\n",), close_error)
    send_started = anyio.Event()

    async def receive() -> dict[str, object]:
        await send_started.wait()
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        send_started.set()
        await anyio.sleep_forever()

    with pytest.raises(RuntimeError) as raised:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
            receive=receive,
            send=send,
        )

    assert raised.value is persistence_error
    assert attempts.events == ["terminal_persist"]
    assert response.close_count == 1


async def test_disconnect_terminal_is_not_replaced_by_ordinary_close_failure() -> None:
    close_error = RuntimeError("synthetic post-disconnect close failure")
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _CloseFailureResponse((b"data: [DONE]\n\n",), close_error)
    send_started = anyio.Event()

    async def receive() -> dict[str, object]:
        await send_started.wait()
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        send_started.set()
        await anyio.sleep_forever()

    await ChatStreamResponder(supervisor).run_stream(
        routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
        receive=receive,
        send=send,
    )

    assert response.close_count == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED


async def test_send_os_terminal_is_not_replaced_by_ordinary_close_failure() -> None:
    close_error = RuntimeError("synthetic post-send-os close failure")
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _CloseFailureResponse((b"data: [DONE]\n\n",), close_error)

    async def send(_message: dict[str, object]) -> None:
        raise OSError

    await ChatStreamResponder(supervisor).run_stream(
        routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
        receive=_receive_forever,
        send=send,
    )

    assert response.close_count == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED


async def test_failed_stream_close_consumes_physical_close_ownership_once() -> None:
    close_error = RuntimeError("synthetic close failure")
    response = _CloseFailureResponse((), close_error)
    stream = _stream_terminal(response).stream

    with pytest.raises(RuntimeError) as raised:
        await stream.aclose()
    await stream.aclose()

    assert raised.value is close_error
    assert response.close_count == 1


async def test_terminal_persistence_exception_closes_then_reraises_same_instance() -> None:
    error = RuntimeError("synthetic terminal persistence failure")
    attempts = _FailingAttempts([], error)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _Response((b"data: [DONE]\n\n",))
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    with pytest.raises(RuntimeError) as raised:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
            receive=_receive_forever,
            send=send,
        )

    assert raised.value is error
    assert response.close_count == 1
    assert len(sent) == 2
    assert attempts.events == ["terminal_persist"]


async def test_explicit_disconnect_interrupts_hanging_downstream_send() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _Response((b"data: [DONE]\n\n",))
    send_started = anyio.Event()

    async def receive() -> dict[str, object]:
        await send_started.wait()
        return {"type": "http.disconnect"}

    async def send(_message: dict[str, object]) -> None:
        send_started.set()
        await anyio.sleep_forever()

    with anyio.fail_after(1):
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
            receive=receive,
            send=send,
        )

    assert response.close_count == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED


async def test_disconnect_between_outer_and_scheduled_send_check_prevents_send() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    send_calls = 0

    async def send(_message: dict[str, object]) -> None:
        nonlocal send_calls
        send_calls += 1

    context = StreamContext(
        RoutedStream(_stream_terminal(), _lease(), 1, 1.0),
        send,
        supervisor,
        _SequencedDisconnect(),
    )

    with pytest.raises(DownstreamDisconnectedError):
        await send_or_stop(context, sse_response_start(), StreamState())

    assert send_calls == 0


async def test_disconnect_between_outer_and_scheduled_read_check_prevents_anext() -> None:
    iterator = _CountingFrameIterator()

    disconnected, frame = await next_frame_or_disconnect(
        iterator,
        _SequencedDisconnect(),
    )

    assert disconnected is True
    assert frame is None
    assert iterator.calls == 0


async def test_losing_send_exception_is_suppressed_by_explicit_disconnect_winner() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    send_started = anyio.Event()
    release_send = anyio.Event()
    losing_error = RuntimeError("synthetic losing send error")
    outcomes: list[str] = []

    async def send(_message: dict[str, object]) -> None:
        send_started.set()
        await release_send.wait()
        raise losing_error

    context = StreamContext(
        RoutedStream(_stream_terminal(), _lease(), 1, 1.0),
        send,
        supervisor,
        anyio.Event(),
    )

    async def run_send() -> None:
        try:
            await send_or_stop(context, sse_response_start(), StreamState())
        except DownstreamTerminalStopError:
            outcomes.append("terminal_stop")

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(run_send)
        await send_started.wait()
        _ = await supervisor.coordinate(
            TerminalProposal.for_kind(TerminalKind.EXPLICIT_DISCONNECT, sequence=0),
            generation=0,
        )
        release_send.set()

    assert outcomes == ["terminal_stop"]
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED


async def test_explicit_disconnect_interrupts_hanging_upstream_and_closes_it() -> None:
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    terminal, response = _hanging_stream_terminal()
    routed = RoutedStream(terminal, _lease(), 1, 1.0)
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        await response.iterator_waiting.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    with anyio.fail_after(1):
        await ChatStreamResponder(supervisor).run_stream(
            routed=routed,
            receive=receive,
            send=send,
        )

    assert response.close_count == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]


async def test_live_sse_transport_failure_has_typed_persistence_and_safe_wire() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _TransportFailureResponse(())
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    status = await ChatStreamResponder(supervisor).run_stream_status(
        routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
        receive=_receive_forever,
        send=send,
    )

    assert status is LastStatusClass.TIMEOUT
    assert response.close_count == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.TIMEOUT
    terminal_body = sent[-1]["body"]
    assert isinstance(terminal_body, bytes)
    assert terminal_body == (
        b'event: error\ndata: {"error":{"code":"upstream_stream_error",'
        b'"message":"upstream stream ended unexpectedly","request_id":"request"}}\n\n'
    )
    assert sent[-1]["more_body"] is False


@pytest.mark.parametrize(
    ("primary", "expected_status"),
    [
        (
            NvidiaTransportError(TransportErrorCode.READ_TIMEOUT, 1),
            LastStatusClass.TIMEOUT,
        ),
        (OSError(), LastStatusClass.UPSTREAM_PROTOCOL_ERROR),
    ],
    ids=("typed_read_timeout", "os_protocol_failure"),
)
async def test_stream_primary_survives_unresolved_retirement_and_fail_stops(
    monkeypatch: pytest.MonkeyPatch,
    primary: Exception,
    expected_status: LastStatusClass,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _PrimaryFailureUnresolvedCloseResponse(primary)
    fail_stop = _FailStop()
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    with pytest.raises(ResponseRetirementUnresolvedError):
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(
                _stream_terminal(response, fail_stop),
                _lease(),
                1,
                1.0,
            ),
            receive=_receive_forever,
            send=send,
        )

    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    assert response.close_count == 1
    assert fail_stop.calls == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is expected_status
    assert sent[-1]["more_body"] is True
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


@pytest.mark.parametrize(
    ("primary", "expected_status"),
    [
        (
            NvidiaTransportError(TransportErrorCode.READ_TIMEOUT, 1),
            LastStatusClass.TIMEOUT,
        ),
        (OSError(), LastStatusClass.UPSTREAM_PROTOCOL_ERROR),
        (RepresentationProtocolError(), LastStatusClass.UPSTREAM_PROTOCOL_ERROR),
    ],
    ids=("typed_read_timeout", "os_protocol_failure", "representation_failure"),
)
async def test_real_fail_stop_waits_for_stream_terminal_persistence(
    monkeypatch: pytest.MonkeyPatch,
    primary: Exception,
    expected_status: LastStatusClass,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _PrimaryFailureUnresolvedCloseResponse(primary)
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(_Readiness(events), root_scope)
    with pytest.raises(ResponseRetirementUnresolvedError), root_scope:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(
                _stream_terminal(response, fail_stop),
                _lease(),
                1,
                1.0,
            ),
            receive=_receive_forever,
            send=send,
        )

    assert events[:3] == ["terminal_persist", "ready:false"]
    assert root_scope.cancel_called is True
    assert fail_stop.triggered is True
    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is expected_status
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


@pytest.mark.parametrize(
    "drift_error",
    [
        pytest.param(PinnedRuntimeDriftError(), id="runtime_drift"),
        pytest.param(PinnedTransportDriftError(), id="transport_drift"),
    ],
)
async def test_stream_drift_persists_terminal_before_process_fail_stop(
    drift_error: Exception,
) -> None:
    events: list[str] = []
    attempts = _Attempts(events)
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _PrimaryFailureResponse(drift_error)
    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(_Readiness(events), root_scope)

    async def send(_message: dict[str, object]) -> None:
        return

    with pytest.raises(ResponseRetirementUnresolvedError), root_scope:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(
                _stream_terminal(response, fail_stop),
                _lease(),
                1,
                1.0,
            ),
            receive=_receive_forever,
            send=send,
        )

    assert events == ["terminal_persist", "ready:false"]
    assert attempts.commands[0].status_class is LastStatusClass.UPSTREAM_PROTOCOL_ERROR
    assert response.close_count == 1
    assert root_scope.cancel_called is True
    assert fail_stop.triggered is True


async def test_live_sse_protocol_failure_emits_one_request_correlated_error_event() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    response = _Response(())
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    status = await ChatStreamResponder(supervisor).run_stream_status(
        routed=RoutedStream(_stream_terminal(response), _lease(), 1, 1.0),
        receive=_receive_forever,
        send=send,
    )

    assert status is LastStatusClass.UPSTREAM_PROTOCOL_ERROR
    assert response.close_count == 1
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.UPSTREAM_PROTOCOL_ERROR
    assert sent[-1] == {
        "type": "http.response.body",
        "body": (
            b'event: error\ndata: {"error":{"code":"upstream_stream_error",'
            b'"message":"upstream stream ended unexpectedly","request_id":"request"}}\n\n'
        ),
        "more_body": False,
    }


async def test_json_stream_conversion_sends_data_then_done_without_trailing_empty_body() -> None:
    terminal = JsonTerminal(
        representation=JsonRepresentation(
            raw=b'{"id":"r","choices":[],"usage":{}}',
            value={"id": "r", "choices": [], "usage": {}},
        ),
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.JSON,
            content_length=None,
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"application/json"),),
        ),
    )
    routed = RoutedJson(
        terminal,
        uuid4(),
        1,
        json_to_sse_frames(terminal.representation),
    )
    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await ChatStreamResponder(None).run_json(routed=routed, send=send, requested_stream=True)

    assert len(sent) == 3
    data_body = sent[1]["body"]
    assert isinstance(data_body, bytes)
    assert data_body.startswith(b"data: ")
    assert sent[1]["more_body"] is True
    assert sent[2] == {
        "type": "http.response.body",
        "body": b"data: [DONE]\n\n",
        "more_body": False,
    }


async def test_downstream_os_error_stops_without_network_reclassification() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )

    async def send(_message: dict[str, object]) -> None:
        raise OSError

    await ChatStreamResponder(supervisor).run_stream(
        routed=RoutedStream(_stream_terminal(), _lease(), 1, 1.0),
        receive=_receive_forever,
        send=send,
    )

    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.CANCELLED


async def test_downstream_exception_persists_delivery_failure_and_reraises_same_instance() -> None:
    attempts = _Attempts([])
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    error = RuntimeError("synthetic downstream failure")

    async def send(_message: dict[str, object]) -> None:
        raise error

    with pytest.raises(RuntimeError) as raised:
        await ChatStreamResponder(supervisor).run_stream(
            routed=RoutedStream(_stream_terminal(), _lease(), 1, 1.0),
            receive=_receive_forever,
            send=send,
        )

    assert raised.value is error
    assert len(attempts.commands) == 1
    assert attempts.commands[0].status_class is LastStatusClass.DELIVERY_FAILED
