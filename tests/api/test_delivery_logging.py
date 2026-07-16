"""Durable JSON terminal logging remains independent from ASGI delivery."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import anyio
import orjson
import pytest
from fastapi import Request
from starlette.types import Message, Scope

import nvidia_build_lb.api_routes as api_routes_module
from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.api_types import StreamLogContext
from nvidia_build_lb.logging import StructuredLogLine
from nvidia_build_lb.main import create_app
from nvidia_build_lb.polling import NvidiaSSEStream
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.schemas import ChatCompletionsRequest
from nvidia_build_lb.streaming import ChatStreamResponder
from tests.contracts._support import CHAT_TOKEN, ENABLED_KEY_ID

from .fakes import ApiHarness, build_live_fake_upstream_harness

pytestmark = [pytest.mark.api, pytest.mark.anyio]


class _DeliveryError(Exception):
    """Synthetic downstream body-send failure."""


class _ResponderConstructionError(Exception):
    """Synthetic response-construction failure before stream handoff."""


@dataclass(frozen=True, slots=True)
class _FailingResponderFactory:
    error: _ResponderConstructionError

    def create(self, stream_log: StreamLogContext | None = None) -> ChatStreamResponder:
        del stream_log
        raise self.error


def _scope(body: bytes) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"",
        "root_path": "",
        "headers": (
            (b"host", b"127.0.0.1:2456"),
            (b"authorization", f"Bearer {CHAT_TOKEN}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ),
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 2456),
        "state": {},
    }


def _chat_body(*, stream: bool) -> bytes:
    return orjson.dumps(
        {
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "delivery failure probe"}],
            "stream": stream,
        }
    )


def _connected_receive(body: bytes) -> Callable[[], Awaitable[Message]]:
    received = False

    async def receive() -> Message:
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await anyio.sleep_forever()
        raise RuntimeError

    return receive


def _record_messages(messages: list[Message]) -> Callable[[Message], Awaitable[None]]:
    async def send(message: Message) -> None:
        messages.append(message)

    return send


async def test_nonstream_send_failure_does_not_reclassify_durable_success(
    api_harness: ApiHarness,
) -> None:
    body = _chat_body(stream=False)
    app = create_app(api_harness.services)
    received = False

    async def receive() -> Message:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise _DeliveryError

    with pytest.raises(_DeliveryError):
        await app(_scope(body), receive, send)

    assert api_harness.services.active_requests.snapshot() == frozenset()
    lines = api_harness.log_stream.getvalue().splitlines()
    assert len(lines) == 1
    event = StructuredLogLine.model_validate_json(lines[0])
    assert str(event.internal_key_id) == ENABLED_KEY_ID
    assert event.attempt_ordinal == 1
    assert event.safe_status_class is LastStatusClass.SUCCESS
    assert event.terminal_outcome is TerminalOutcome.SUCCEEDED


@pytest.mark.parametrize("stream", [False, True], ids=("json", "sse"))
async def test_active_request_is_held_until_final_body_send_completes(
    api_harness: ApiHarness,
    stream: bool,
) -> None:
    body = _chat_body(stream=stream)
    app = create_app(api_harness.services)
    final_send_started = anyio.Event()
    allow_final_send = anyio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            final_send_started.set()
            await allow_final_send.wait()

    async def run_request() -> None:
        await app(_scope(body), _connected_receive(body), send)

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(run_request)
        await final_send_started.wait()
        assert len(api_harness.services.active_requests.snapshot()) == 1
        allow_final_send.set()

    assert api_harness.services.active_requests.snapshot() == frozenset()


async def test_composed_stream_preserves_routing_owned_asgi_frames(
    api_harness: ApiHarness,
) -> None:
    body = _chat_body(stream=True)
    sent: list[Message] = []

    await create_app(api_harness.services)(
        _scope(body),
        _connected_receive(body),
        _record_messages(sent),
    )

    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
        "http.response.body",
    ]
    assert sent[1]["more_body"] is True
    assert sent[2]["body"] == b"data: [DONE]\n\n"
    assert sent[2]["more_body"] is False


@pytest.mark.parametrize(
    ("error", "reraises", "status", "outcome"),
    [
        pytest.param(
            OSError(),
            False,
            LastStatusClass.CANCELLED,
            TerminalOutcome.CANCELLED,
            id="os-error",
        ),
        pytest.param(
            _DeliveryError(),
            True,
            LastStatusClass.DELIVERY_FAILED,
            TerminalOutcome.FAILED,
            id="generic-error",
        ),
    ],
)
async def test_composed_live_stream_classifies_actual_send_failure(
    error: Exception,
    reraises: bool,
    status: LastStatusClass,
    outcome: TerminalOutcome,
) -> None:
    harness = build_live_fake_upstream_harness()
    app = create_app(harness.services)
    first_body = _chat_body(stream=False)
    stream_body = _chat_body(stream=True)
    try:
        first_sent: list[Message] = []
        await app(
            _scope(first_body),
            _connected_receive(first_body),
            _record_messages(first_sent),
        )

        async def fail_body_send(message: Message) -> None:
            if message["type"] == "http.response.body":
                raise error

        if reraises:
            with pytest.raises(type(error)) as captured:
                await app(_scope(stream_body), _connected_receive(stream_body), fail_body_send)
            assert captured.value is error
        else:
            await app(_scope(stream_body), _connected_receive(stream_body), fail_body_send)

        terminal = harness.attempts.terminals[-1]
        assert terminal.status_class is status
        assert terminal.outcome is outcome
    finally:
        await harness.close()


async def test_live_sse_final_send_failure_preserves_durable_success_and_registry() -> None:
    harness = build_live_fake_upstream_harness()
    app = create_app(harness.services)
    first_body = _chat_body(stream=False)
    stream_body = _chat_body(stream=True)
    error = _DeliveryError()
    try:
        await app(
            _scope(first_body),
            _connected_receive(first_body),
            _record_messages([]),
        )

        async def fail_final_send(message: Message) -> None:
            if message["type"] != "http.response.body" or message.get("more_body", False):
                return
            terminal = harness.attempts.terminals[-1]
            assert terminal.status_class is LastStatusClass.SUCCESS
            assert terminal.outcome is TerminalOutcome.SUCCEEDED
            assert len(harness.services.active_requests.snapshot()) == 1
            raise error

        with pytest.raises(_DeliveryError) as captured:
            await app(
                _scope(stream_body),
                _connected_receive(stream_body),
                fail_final_send,
            )

        assert captured.value is error
        terminal = harness.attempts.terminals[-1]
        assert terminal.status_class is LastStatusClass.SUCCESS
        assert terminal.outcome is TerminalOutcome.SUCCEEDED
        assert harness.services.active_requests.snapshot() == frozenset()
    finally:
        await harness.close()


async def test_responder_construction_failure_cancels_unhanded_live_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = build_live_fake_upstream_harness()
    first_body = _chat_body(stream=False)
    stream_body = _chat_body(stream=True)
    error = _ResponderConstructionError()
    closed_streams: list[NvidiaSSEStream] = []
    original_close = NvidiaSSEStream.aclose

    async def record_close(stream: NvidiaSSEStream) -> None:
        closed_streams.append(stream)
        await original_close(stream)

    def fixed_request_id(_request: Request) -> str:
        return "responder-construction-failure"

    try:
        app = create_app(harness.services)
        await app(
            _scope(first_body),
            _connected_receive(first_body),
            _record_messages([]),
        )
        monkeypatch.setattr(NvidiaSSEStream, "aclose", record_close)
        monkeypatch.setattr(api_routes_module, "request_id_from", fixed_request_id)
        failing_services = replace(
            harness.services,
            responders=_FailingResponderFactory(error),
        )
        request = Request(_scope(stream_body), receive=_connected_receive(stream_body))
        payload = ChatCompletionsRequest.model_validate_json(stream_body)

        with pytest.raises(_ResponderConstructionError) as captured:
            _ = await api_routes_module.build_public_chat_response(
                request,
                payload,
                failing_services,
            )

        assert captured.value is error
        terminal = harness.attempts.terminals[-1]
        assert terminal.status_class is LastStatusClass.CANCELLED
        assert terminal.outcome is TerminalOutcome.CANCELLED
        assert len(closed_streams) == 1
        assert harness.services.active_requests.snapshot() == frozenset()
    finally:
        await harness.close()
