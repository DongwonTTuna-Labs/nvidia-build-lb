"""Production admin probe maps only durable routed outcomes."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import override
from uuid import UUID

import pytest
from pydantic import SecretStr

from nvidia_build_lb.admin.schemas import LastStatusClass, ProbeStatus, UpstreamProbeResponse
from nvidia_build_lb.api_types import StreamLogContext
from nvidia_build_lb.asgi_response import AsgiReceive, AsgiSend
from nvidia_build_lb.attempt_types import AttemptIdentity, AttemptLease
from nvidia_build_lb.header_types import MediaType, ValidatedUpstreamHeaders
from nvidia_build_lb.outcome_types import SourceSignal
from nvidia_build_lb.outcomes import (
    HttpStatusSignal,
    LedgerCapacityExhausted,
    ReservationFailure,
    map_public_outcome,
)
from nvidia_build_lb.polling import FailureTerminal, NvidiaSSEStream, StreamTerminal
from nvidia_build_lb.polling_types import PrimedSSEState
from nvidia_build_lb.probe_errors import ProbeNotExecutedError
from nvidia_build_lb.production_probe import RoutingProbeExecutor
from nvidia_build_lb.routing import RoutedFailure, RoutedStream
from nvidia_build_lb.sse import SSEFrameParser
from nvidia_build_lb.streaming import ChatStreamResponder
from tests.contracts.fakes import FakeRouter

pytestmark = pytest.mark.anyio

_KEY_ID = UUID("00000000-0000-4000-8000-000000000001")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(slots=True)
class _Upstream:
    observed: list[ProbeStatus]

    async def probe_result(self, key_id: UUID, status: ProbeStatus) -> UpstreamProbeResponse:
        assert key_id == _KEY_ID
        self.observed.append(status)
        return UpstreamProbeResponse(
            id=key_id,
            enabled=False,
            probe_status=status,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


@dataclass(frozen=True, slots=True)
class _FailureRouter:
    status_code: int

    async def cancel_unhanded_stream(self, routed: RoutedStream) -> None:
        await routed.terminal.stream.aclose()

    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedFailure:
        del request_id, body, requested_stream
        assert explicit_probe_key_id == _KEY_ID
        return RoutedFailure(
            FailureTerminal(map_public_outcome(HttpStatusSignal(self.status_code)), None),
            1,
        )


@dataclass(frozen=True, slots=True)
class _UnadmittedRouter:
    signal: SourceSignal

    async def cancel_unhanded_stream(self, routed: RoutedStream) -> None:
        await routed.terminal.stream.aclose()

    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedFailure:
        del request_id, body, requested_stream
        assert explicit_probe_key_id == _KEY_ID
        return RoutedFailure(
            FailureTerminal(map_public_outcome(self.signal), None),
            0,
        )


class _Response:
    status_code: int = 200
    raw_headers: tuple[tuple[bytes, bytes], ...] = ()

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        yield b""

    async def aclose(self) -> None:
        return


class _FailStop:
    def trigger(self) -> None:
        return


def _stream_terminal() -> StreamTerminal:
    response = _Response()
    stream = NvidiaSSEStream(
        response=response,
        iterator=response.aiter_raw().__aiter__(),
        state=PrimedSSEState(SSEFrameParser(max_frame_bytes=1024), (), None),
        fail_stop=_FailStop(),
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


@dataclass(slots=True)
class _StreamRouter:
    cancelled: list[RoutedStream] = field(default_factory=list)

    async def cancel_unhanded_stream(self, routed: RoutedStream) -> None:
        self.cancelled.append(routed)
        await routed.terminal.stream.aclose()

    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedStream:
        del body
        assert requested_stream is False
        assert explicit_probe_key_id == _KEY_ID
        identity = AttemptIdentity(
            UUID("00000000-0000-4000-8000-000000000011"),
            UUID("00000000-0000-4000-8000-000000000012"),
            request_id,
            UUID("00000000-0000-4000-8000-000000000013"),
            _NOW,
        )
        lease = AttemptLease(identity, _KEY_ID, SecretStr("synthetic"))
        return RoutedStream(_stream_terminal(), lease, 1, 1.0)


class _StatusResponder(ChatStreamResponder):
    status: LastStatusClass

    def __init__(self, status: LastStatusClass) -> None:
        super().__init__(None)
        self.status = status

    @override
    async def run_stream_status(
        self,
        *,
        routed: RoutedStream,
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> LastStatusClass:
        del routed, receive, send
        return self.status


@dataclass(frozen=True, slots=True)
class _Responders:
    status: LastStatusClass

    def create(self, stream_log: StreamLogContext | None = None) -> ChatStreamResponder:
        assert stream_log == StreamLogContext(_KEY_ID, 1)
        return _StatusResponder(self.status)


class _ResponderConstructionError(Exception):
    """Synthetic probe responder-construction failure."""


@dataclass(frozen=True, slots=True)
class _FailingResponders:
    error: _ResponderConstructionError

    def create(self, stream_log: StreamLogContext | None = None) -> ChatStreamResponder:
        del stream_log
        raise self.error


async def test_probe_success_is_projected_as_valid() -> None:
    upstream = _Upstream([])

    result = await RoutingProbeExecutor(FakeRouter(), upstream).probe(_KEY_ID, "probe-request")

    assert result.probe_status is ProbeStatus.VALID
    assert upstream.observed == [ProbeStatus.VALID]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, ProbeStatus.INVALID_CREDENTIAL),
        (429, ProbeStatus.RATE_LIMITED),
        (503, ProbeStatus.UPSTREAM_UNAVAILABLE),
    ],
)
async def test_probe_failure_uses_closed_safe_status(
    status_code: int,
    expected: ProbeStatus,
) -> None:
    upstream = _Upstream([])

    result = await RoutingProbeExecutor(_FailureRouter(status_code), upstream).probe(
        _KEY_ID,
        "probe-request",
    )

    assert result.probe_status is expected
    assert upstream.observed == [expected]


@pytest.mark.parametrize(
    "signal",
    [
        pytest.param(LedgerCapacityExhausted(), id="ledger-capacity"),
        pytest.param(ReservationFailure(), id="reservation"),
    ],
)
async def test_probe_without_a_durable_attempt_is_not_success_shaped(
    signal: SourceSignal,
) -> None:
    upstream = _Upstream([])

    with pytest.raises(ProbeNotExecutedError) as captured:
        _ = await RoutingProbeExecutor(_UnadmittedRouter(signal), upstream).probe(
            _KEY_ID,
            "probe-not-admitted",
        )

    assert captured.value.terminal.outcome == map_public_outcome(signal)
    assert upstream.observed == []


@pytest.mark.parametrize(
    ("persisted", "expected"),
    [
        (LastStatusClass.SUCCESS, ProbeStatus.VALID),
        (LastStatusClass.INVALID_CREDENTIAL, ProbeStatus.INVALID_CREDENTIAL),
        (LastStatusClass.RATE_LIMITED, ProbeStatus.RATE_LIMITED),
        (LastStatusClass.TIMEOUT, ProbeStatus.UPSTREAM_UNAVAILABLE),
        (LastStatusClass.UPSTREAM_PROTOCOL_ERROR, ProbeStatus.UPSTREAM_UNAVAILABLE),
    ],
)
async def test_streamed_probe_uses_durable_terminal_status(
    persisted: LastStatusClass,
    expected: ProbeStatus,
) -> None:
    upstream = _Upstream([])

    result = await RoutingProbeExecutor(
        _StreamRouter(),
        upstream,
        _Responders(persisted),
    ).probe(_KEY_ID, "stream-probe-request")

    assert result.probe_status is expected
    assert upstream.observed == [expected]


async def test_streamed_probe_cancels_when_responder_construction_fails() -> None:
    router = _StreamRouter()
    error = _ResponderConstructionError()

    with pytest.raises(_ResponderConstructionError) as captured:
        _ = await RoutingProbeExecutor(
            router,
            _Upstream([]),
            _FailingResponders(error),
        ).probe(_KEY_ID, "stream-probe-construction-failure")

    assert captured.value is error
    assert len(router.cancelled) == 1
