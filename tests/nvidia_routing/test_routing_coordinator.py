"""Sequential two-key routing, persistence-before-failover, and cooldowns."""

import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import override
from uuid import UUID

import anyio
import pytest
from pydantic import SecretStr

import nvidia_build_lb.response_retirement as response_retirement_module
from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.attempt_fail_stop import AttemptFailStop, LifecycleAttemptFailStop
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptLease,
    AttemptStartCommand,
    CooldownKind,
    TerminalCommitted,
)
from nvidia_build_lb.headers import MediaType, ValidatedUpstreamHeaders
from nvidia_build_lb.nvidia_adapter import NvidiaAdapterDependencies, NvidiaHostedAdapter
from nvidia_build_lb.outcomes import (
    HttpStatusSignal,
    NoEligibleKey,
    PollDeadline,
    map_public_outcome,
)
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.polling import (
    FailureTerminal,
    JsonTerminal,
    NvidiaSSEStream,
    ResponseRetirementUnresolvedError,
    StreamTerminal,
)
from nvidia_build_lb.polling_types import PrimedSSEState
from nvidia_build_lb.representations import JsonRepresentation
from nvidia_build_lb.request_wire import NvidiaWireRequest
from nvidia_build_lb.routing import (
    RoutedFailure,
    RoutedJson,
    RoutedStream,
    RoutingCoordinator,
    RoutingCoordinatorDependencies,
)
from nvidia_build_lb.routing_models import HostedAdapter
from nvidia_build_lb.scheduler_state import NoEligibleUpstreamKeyError, TerminalOutcome
from nvidia_build_lb.scheduler_types import NoEligibleReason
from nvidia_build_lb.sse import SSEFrameParser
from nvidia_build_lb.transport_common import PinnedTransportDriftError

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_EPOCH = UUID("00000000-0000-0000-0000-000000000500")
_KEY_ONE = UUID("00000000-0000-0000-0000-000000000101")
_KEY_TWO = UUID("00000000-0000-0000-0000-000000000102")


class _Clock:
    wall: datetime
    monotonic_value: float

    def __init__(self) -> None:
        self.wall = datetime(2026, 7, 13, tzinfo=UTC)
        self.monotonic_value = 0.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.monotonic_value


class _UuidSource:
    values: deque[UUID]

    def __init__(self) -> None:
        self.values = deque(
            UUID(f"00000000-0000-0000-0000-{value:012d}") for value in range(601, 620)
        )

    def new(self) -> UUID:
        return self.values.popleft()


class _Jitter:
    def uniform(self, lower: float, upper: float) -> float:
        assert lower == upper / 2
        return upper


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


class _RootCancellation:
    events: list[str]
    scope: anyio.CancelScope

    def __init__(self, events: list[str], scope: anyio.CancelScope) -> None:
        self.events = events
        self.scope = scope

    def cancel(self) -> None:
        self.events.append("root:cancel")
        self.scope.cancel()


class _Attempts:
    keys: deque[UUID]
    events: list[str]
    starts: list[AttemptStartCommand]
    terminals: list[AttemptFinalizeCommand]

    def __init__(self, keys: tuple[UUID, ...]) -> None:
        self.keys = deque(keys)
        self.events = []
        self.starts = []
        self.terminals = []

    async def reserve_attempt(self, command: AttemptStartCommand) -> AttemptLease:
        self.events.append("reserve")
        self.starts.append(command)
        if not self.keys:
            raise NoEligibleUpstreamKeyError
        key_id = self.keys.popleft()
        return AttemptLease(command.identity, key_id, SecretStr(f"secret-{key_id}"))

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        self.events.append("finalize")
        self.terminals.append(command)
        return TerminalCommitted(command.identity)


class _ClassifiedNoEligibleAttempts(_Attempts):
    reason: NoEligibleReason
    retry_after_seconds: int | None

    def __init__(
        self,
        keys: tuple[UUID, ...],
        reason: NoEligibleReason,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(keys)
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds

    @override
    async def reserve_attempt(self, command: AttemptStartCommand) -> AttemptLease:
        if self.keys:
            return await super().reserve_attempt(command)
        self.events.append("reserve")
        self.starts.append(command)
        raise NoEligibleUpstreamKeyError(self.reason, self.retry_after_seconds)


class _StreakAttempts(_Attempts):
    rate_streak: int
    transient_streak: int

    def __init__(self, *, rate_streak: int = 0, transient_streak: int = 0) -> None:
        super().__init__((_KEY_ONE,))
        self.rate_streak = rate_streak
        self.transient_streak = transient_streak

    @override
    async def reserve_attempt(self, command: AttemptStartCommand) -> AttemptLease:
        lease = await super().reserve_attempt(command)
        return AttemptLease(
            lease.identity,
            lease.key_id,
            lease.credential,
            self.rate_streak,
            self.transient_streak,
        )


class _Adapter:
    results: deque[JsonTerminal | StreamTerminal | FailureTerminal]
    credentials: list[SecretStr]
    events: list[str]

    def __init__(
        self,
        results: tuple[JsonTerminal | StreamTerminal | FailureTerminal, ...],
        events: list[str],
    ) -> None:
        self.results = deque(results)
        self.credentials = []
        self.events = events

    async def execute(
        self,
        *,
        credential: SecretStr,
        body: bytes,
    ) -> JsonTerminal | StreamTerminal | FailureTerminal:
        assert body == b"{}"
        self.events.append("network")
        self.credentials.append(credential)
        return self.results.popleft()


class _BlockingAdapter:
    started: anyio.Event

    def __init__(self) -> None:
        self.started = anyio.Event()

    async def execute(
        self,
        *,
        credential: SecretStr,
        body: bytes,
    ) -> JsonTerminal | StreamTerminal | FailureTerminal:
        del credential, body
        self.started.set()
        await anyio.sleep_forever()
        raise RuntimeError


class _RaisingIterator:
    error: Exception
    event: str
    events: list[str]

    def __init__(self, error: Exception, event: str, events: list[str]) -> None:
        self.error = error
        self.event = event
        self.events = events

    def __aiter__(self) -> "_RaisingIterator":
        return self

    async def __anext__(self) -> bytes:
        self.events.append(self.event)
        raise self.error


class _DriftReadCloseResponse:
    status_code: int = 200
    raw_headers: tuple[tuple[bytes, bytes], ...] = ((b"Content-Type", b"application/json"),)
    primary: Exception
    cleanup: Exception
    events: list[str]
    close_count: int

    def __init__(
        self,
        *,
        primary: Exception,
        cleanup: Exception,
        events: list[str],
    ) -> None:
        self.primary = primary
        self.cleanup = cleanup
        self.events = events
        self.close_count = 0

    def aiter_raw(self) -> AsyncIterator[bytes]:
        return _RaisingIterator(self.primary, "read:drift", self.events)

    async def aclose(self) -> None:
        self.close_count += 1
        self.events.append("close:drift")
        raise self.cleanup


class _Origin202Response:
    status_code: int = 202
    raw_headers: tuple[tuple[bytes, bytes], ...] = ((b"NVCF-REQID", b"request-202"),)
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def aiter_raw(self) -> AsyncIterator[bytes]:
        return _RaisingIterator(AssertionError(), "read:origin", self.events)

    async def aclose(self) -> None:
        self.events.append("close:origin")


class _DriftClose202Response:
    status_code: int = 202
    raw_headers: tuple[tuple[bytes, bytes], ...] = ((b"NVCF-REQID", b"request-202"),)
    error: Exception
    events: list[str]
    close_count: int

    def __init__(self, error: Exception, events: list[str]) -> None:
        self.error = error
        self.events = events
        self.close_count = 0

    def aiter_raw(self) -> AsyncIterator[bytes]:
        return _RaisingIterator(AssertionError(), "read:unexpected", self.events)

    async def aclose(self) -> None:
        self.close_count += 1
        self.events.append("close:drift")
        raise self.error


class _HangingClose202Response:
    status_code: int = 202
    raw_headers: tuple[tuple[bytes, bytes], ...] = ((b"NVCF-REQID", b"request-202"),)
    events: list[str]
    close_count: int
    close_finished: anyio.Event
    close_started: anyio.Event
    release: anyio.Event

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.close_count = 0
        self.close_finished = anyio.Event()
        self.close_started = anyio.Event()
        self.release = anyio.Event()

    def aiter_raw(self) -> AsyncIterator[bytes]:
        return _RaisingIterator(AssertionError(), "read:unexpected", self.events)

    async def aclose(self) -> None:
        self.close_count += 1
        self.events.append("close:start")
        self.close_started.set()
        with anyio.CancelScope(shield=True):
            await self.release.wait()
        self.events.append("close:finish")
        self.close_finished.set()


class _HangingReadCloseResponse:
    status_code: int = 200
    raw_headers: tuple[tuple[bytes, bytes], ...] = ((b"Content-Type", b"application/json"),)
    events: list[str]
    close_count: int
    close_finished: anyio.Event
    close_started: anyio.Event
    read_started: anyio.Event
    release: anyio.Event

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.close_count = 0
        self.close_finished = anyio.Event()
        self.close_started = anyio.Event()
        self.read_started = anyio.Event()
        self.release = anyio.Event()

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.events.append("read:start")
        self.read_started.set()
        await anyio.sleep_forever()
        yield b""

    async def aclose(self) -> None:
        self.close_count += 1
        self.events.append("close:start")
        self.close_started.set()
        with anyio.CancelScope(shield=True):
            await self.release.wait()
        self.events.append("close:finish")
        self.close_finished.set()


class _DriftReadCloseClient:
    responses: deque[
        _Origin202Response
        | _DriftClose202Response
        | _DriftReadCloseResponse
        | _HangingClose202Response
        | _HangingReadCloseResponse
    ]
    events: list[str]

    def __init__(
        self,
        responses: tuple[
            _Origin202Response
            | _DriftClose202Response
            | _DriftReadCloseResponse
            | _HangingClose202Response
            | _HangingReadCloseResponse,
            ...,
        ],
        events: list[str],
    ) -> None:
        self.responses = deque(responses)
        self.events = events

    async def send(
        self,
        request: NvidiaWireRequest,
        *,
        timeout_seconds: float,
    ) -> (
        _Origin202Response
        | _DriftClose202Response
        | _DriftReadCloseResponse
        | _HangingClose202Response
        | _HangingReadCloseResponse
    ):
        assert timeout_seconds > 0
        self.events.append(f"network:{request.method}")
        return self.responses.popleft()


class _Sleeper:
    async def sleep(self, seconds: float) -> None:
        assert seconds >= 0


class _DriftAdapter(_Adapter):
    error: Exception

    def __init__(self, error: Exception, events: list[str]) -> None:
        super().__init__((), events)
        self.error = error

    @override
    async def execute(
        self,
        *,
        credential: SecretStr,
        body: bytes,
    ) -> JsonTerminal | StreamTerminal | FailureTerminal:
        del credential, body
        self.events.append("network")
        raise self.error


class _CancelOnReturnAdapter(_Adapter):
    scope: anyio.CancelScope | None

    def __init__(self, terminal: StreamTerminal, events: list[str]) -> None:
        super().__init__((terminal,), events)
        self.scope = None

    @override
    async def execute(
        self,
        *,
        credential: SecretStr,
        body: bytes,
    ) -> JsonTerminal | StreamTerminal | FailureTerminal:
        terminal = await super().execute(credential=credential, body=body)
        if self.scope is None:
            raise AssertionError
        self.scope.cancel()
        return terminal


class _BlockingFinalizeAttempts(_Attempts):
    finalize_started: anyio.Event
    release_finalize: anyio.Event

    def __init__(self) -> None:
        super().__init__((_KEY_ONE,))
        self.finalize_started = anyio.Event()
        self.release_finalize = anyio.Event()

    @override
    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        self.events.append("finalize")
        self.terminals.append(command)
        self.finalize_started.set()
        await self.release_finalize.wait()
        return TerminalCommitted(command.identity)


class _StreamResponse:
    status_code: int = 200
    raw_headers: tuple[tuple[bytes, bytes], ...] = ()
    close_count: int

    def __init__(self) -> None:
        self.close_count = 0

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        yield b"data: [DONE]\n\n"

    async def aclose(self) -> None:
        self.close_count += 1


class _UnresolvedCloseStreamResponse(_StreamResponse):
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


def _stream(
    response: _StreamResponse | None = None,
    fail_stop: _FailStop | None = None,
) -> NvidiaSSEStream:
    response = response or _StreamResponse()
    parser = SSEFrameParser(max_frame_bytes=128)
    initial = parser.feed(b"data: one\n\n")
    return NvidiaSSEStream(
        response=response,
        iterator=response.aiter_raw(),
        state=PrimedSSEState(parser, initial, None),
        fail_stop=fail_stop or _FailStop(),
    )


def _json(*, retirement_unresolved: bool = False) -> JsonTerminal:
    return JsonTerminal(
        representation=JsonRepresentation(raw=b"{}", value={}),
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.JSON,
            content_length=2,
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"application/json"),),
        ),
        retirement_unresolved=retirement_unresolved,
    )


def _stream_convertible_json() -> JsonTerminal:
    raw = b'{"id":"r","choices":[],"usage":{}}'
    return JsonTerminal(
        representation=JsonRepresentation(
            raw=raw,
            value={"id": "r", "choices": [], "usage": {}},
        ),
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.JSON,
            content_length=len(raw),
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"application/json"),),
        ),
    )


def _failure(
    status: int,
    retry_after: int | None = None,
    *,
    retirement_unresolved: bool = False,
) -> FailureTerminal:
    return FailureTerminal(
        outcome=map_public_outcome(HttpStatusSignal(status)),
        retry_after_seconds=retry_after,
        retirement_unresolved=retirement_unresolved,
    )


def _coordinator(
    attempts: _Attempts,
    adapter: HostedAdapter,
    clock: _Clock,
    fail_stop: AttemptFailStop | None = None,
) -> RoutingCoordinator:
    return RoutingCoordinator(
        RoutingCoordinatorDependencies(
            attempts=attempts,
            adapter=adapter,
            clock=clock,
            monotonic_clock=clock,
            uuid_source=_UuidSource(),
            service_epoch=_EPOCH,
            jitter=_Jitter(),
            fail_stop=fail_stop or _FailStop(),
        )
    )


async def test_rate_limit_is_persisted_before_second_key_succeeds() -> None:
    attempts = _Attempts((_KEY_ONE, _KEY_TWO))
    adapter = _Adapter((_failure(429, retry_after=2), _json()), attempts.events)
    clock = _Clock()

    result = await _coordinator(attempts, adapter, clock).execute(
        request_id="request-1",
        body=b"{}",
    )

    assert isinstance(result, RoutedJson)
    assert result.key_id == _KEY_TWO
    assert result.attempt_count == 2
    assert tuple(
        (
            observation.key_id,
            observation.attempt_ordinal,
            observation.safe_status_class,
            observation.terminal_outcome,
        )
        for observation in result.attempt_observations
    ) == (
        (_KEY_ONE, 1, LastStatusClass.RATE_LIMITED, TerminalOutcome.FAILED),
        (_KEY_TWO, 2, LastStatusClass.SUCCESS, TerminalOutcome.SUCCEEDED),
    )
    assert attempts.events == ["reserve", "network", "finalize", "reserve", "network", "finalize"]
    assert attempts.starts[0].excluded_key_ids == frozenset()
    assert attempts.starts[1].excluded_key_ids == frozenset({_KEY_ONE})
    first_terminal = attempts.terminals[0]
    assert first_terminal.status_class is LastStatusClass.RATE_LIMITED
    assert first_terminal.cooldown_kind is CooldownKind.RATE_LIMIT
    assert first_terminal.cooldown_until == datetime(2026, 7, 13, 0, 0, 2, tzinfo=UTC)
    assert attempts.terminals[1].status_class is LastStatusClass.SUCCESS


async def test_unresolved_retirement_failure_is_persisted_before_root_fail_stop() -> None:
    attempts = _Attempts((_KEY_ONE, _KEY_TWO))
    adapter = _Adapter(
        (_failure(429, retry_after=2, retirement_unresolved=True), _json()),
        attempts.events,
    )

    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(attempts.events),
        _RootCancellation(attempts.events, root_scope),
    )
    with pytest.raises(ResponseRetirementUnresolvedError), root_scope:
        _ = await _coordinator(attempts, adapter, _Clock(), fail_stop).execute(
            request_id="retirement-unresolved-rate-limit",
            body=b"{}",
        )

    assert attempts.events == [
        "reserve",
        "network",
        "finalize",
        "ready:false",
        "root:cancel",
    ]
    assert root_scope.cancel_called is True
    assert fail_stop.triggered is True
    assert len(attempts.terminals) == 1
    assert attempts.terminals[0].status_class is LastStatusClass.RATE_LIMITED
    assert len(adapter.credentials) == 1


@pytest.mark.parametrize(
    ("requested_stream", "expected_status"),
    [
        (False, LastStatusClass.SUCCESS),
        (True, LastStatusClass.UPSTREAM_PROTOCOL_ERROR),
    ],
    ids=("json_success", "stream_conversion_protocol_failure"),
)
async def test_unresolved_retirement_json_is_persisted_before_root_fail_stop(
    requested_stream: bool,
    expected_status: LastStatusClass,
) -> None:
    attempts = _Attempts((_KEY_ONE,))
    adapter = _Adapter((_json(retirement_unresolved=True),), attempts.events)

    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(attempts.events),
        _RootCancellation(attempts.events, root_scope),
    )
    with pytest.raises(ResponseRetirementUnresolvedError), root_scope:
        _ = await _coordinator(attempts, adapter, _Clock(), fail_stop).execute(
            request_id="retirement-unresolved-json",
            body=b"{}",
            requested_stream=requested_stream,
        )

    assert attempts.events == [
        "reserve",
        "network",
        "finalize",
        "ready:false",
        "root:cancel",
    ]
    assert root_scope.cancel_called is True
    assert fail_stop.triggered is True
    assert len(attempts.terminals) == 1
    assert attempts.terminals[0].status_class is expected_status


async def test_unresolved_retirement_is_fatal_when_root_cancel_collaborator_is_noop() -> None:
    attempts = _Attempts((_KEY_ONE, _KEY_TWO))
    adapter = _Adapter(
        (_failure(429, retirement_unresolved=True), _json()),
        attempts.events,
    )
    fail_stop = _FailStop()

    with pytest.raises(ResponseRetirementUnresolvedError):
        _ = await _coordinator(attempts, adapter, _Clock(), fail_stop).execute(
            request_id="retirement-unresolved-noop-root",
            body=b"{}",
        )

    assert fail_stop.calls == 1
    assert attempts.events == ["reserve", "network", "finalize"]
    assert len(attempts.terminals) == 1
    assert len(adapter.credentials) == 1


@pytest.mark.parametrize(
    "drift_error",
    [
        pytest.param(PinnedRuntimeDriftError(), id="runtime_drift"),
        pytest.param(PinnedTransportDriftError(), id="transport_drift"),
    ],
)
async def test_request_drift_retires_attempt_before_process_fail_stop(
    drift_error: Exception,
) -> None:
    attempts = _Attempts((_KEY_ONE,))
    adapter = _DriftAdapter(drift_error, attempts.events)
    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(attempts.events),
        _RootCancellation(attempts.events, root_scope),
    )

    with (
        pytest.raises((PinnedRuntimeDriftError, PinnedTransportDriftError)) as captured,
        root_scope,
    ):
        _ = await _coordinator(attempts, adapter, _Clock(), fail_stop).execute(
            request_id="fatal-runtime-drift",
            body=b"{}",
        )

    assert captured.value is drift_error
    assert attempts.events == [
        "reserve",
        "network",
        "finalize",
        "ready:false",
        "root:cancel",
    ]
    assert attempts.terminals[0].status_class is LastStatusClass.CANCELLED
    assert root_scope.cancel_called is True
    assert fail_stop.triggered is True


@pytest.mark.parametrize(
    "drift_error",
    [
        pytest.param(PinnedRuntimeDriftError(), id="runtime_drift"),
        pytest.param(PinnedTransportDriftError(), id="transport_drift"),
    ],
)
@pytest.mark.parametrize("polled", [False, True], ids=("origin_202", "poll_202"))
async def test_real_adapter_202_close_drift_commits_terminal_before_fail_stop(
    drift_error: Exception,
    polled: bool,
) -> None:
    attempts = _Attempts((_KEY_ONE,))
    failing = _DriftClose202Response(drift_error, attempts.events)
    responses: tuple[_Origin202Response | _DriftClose202Response | _DriftReadCloseResponse, ...] = (
        (_Origin202Response(attempts.events), failing) if polled else (failing,)
    )
    client = _DriftReadCloseClient(responses, attempts.events)
    clock = _Clock()
    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(attempts.events),
        _RootCancellation(attempts.events, root_scope),
    )
    adapter = NvidiaHostedAdapter(
        NvidiaAdapterDependencies(
            client=client,
            monotonic_clock=clock,
            wall_clock=clock,
            sleeper=_Sleeper(),
            jitter=_Jitter(),
            fail_stop=fail_stop,
        )
    )

    with pytest.raises(ResponseRetirementUnresolvedError), root_scope:
        _ = await _coordinator(attempts, adapter, clock, fail_stop).execute(
            request_id="real-adapter-202-close-drift",
            body=b"{}",
        )

    expected = ["reserve", "network:POST"]
    if polled:
        expected.extend(("close:origin", "network:GET"))
    expected.extend(("close:drift", "finalize", "ready:false", "root:cancel"))
    assert attempts.events == expected
    assert failing.close_count == 1
    assert attempts.terminals[0].status_class is LastStatusClass.CANCELLED
    assert fail_stop.triggered is True


async def test_cancelled_202_close_commits_terminal_before_fail_stop() -> None:
    attempts = _Attempts((_KEY_ONE,))
    response = _HangingClose202Response(attempts.events)
    client = _DriftReadCloseClient((response,), attempts.events)
    clock = _Clock()
    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(attempts.events),
        _RootCancellation(attempts.events, root_scope),
    )
    adapter = NvidiaHostedAdapter(
        NvidiaAdapterDependencies(
            client=client,
            monotonic_clock=clock,
            wall_clock=clock,
            sleeper=_Sleeper(),
            jitter=_Jitter(),
            fail_stop=fail_stop,
        )
    )
    observed: list[type[BaseException]] = []

    async def execute() -> None:
        try:
            with root_scope:
                _ = await _coordinator(attempts, adapter, clock, fail_stop).execute(
                    request_id="cancelled-202-close",
                    body=b"{}",
                )
        except (ResponseRetirementUnresolvedError, anyio.get_cancelled_exc_class()) as error:
            observed.append(type(error))

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(execute)
        await response.close_started.wait()
        tasks.cancel_scope.cancel()

    expected = [
        "reserve",
        "network:POST",
        "close:start",
        "finalize",
        "ready:false",
        "root:cancel",
    ]
    assert attempts.events == expected
    assert observed == [ResponseRetirementUnresolvedError]
    assert response.close_count == 1
    assert attempts.terminals[0].status_class is LastStatusClass.CANCELLED
    assert fail_stop.triggered is True

    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()
    assert attempts.events == [*expected, "close:finish"]


@pytest.mark.parametrize("polled", [False, True], ids=("direct", "poll"))
async def test_cancelled_body_read_with_unresolved_close_fail_stops_after_terminal(
    monkeypatch: pytest.MonkeyPatch,
    polled: bool,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    attempts = _Attempts((_KEY_ONE,))
    response = _HangingReadCloseResponse(attempts.events)
    responses = (_Origin202Response(attempts.events), response) if polled else (response,)
    client = _DriftReadCloseClient(responses, attempts.events)
    clock = _Clock()
    root_scope = anyio.CancelScope()
    request_scopes: list[anyio.CancelScope] = []
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(attempts.events),
        _RootCancellation(attempts.events, root_scope),
    )
    adapter = NvidiaHostedAdapter(
        NvidiaAdapterDependencies(
            client=client,
            monotonic_clock=clock,
            wall_clock=clock,
            sleeper=_Sleeper(),
            jitter=_Jitter(),
            fail_stop=fail_stop,
        )
    )
    observed: list[type[BaseException]] = []

    async def execute() -> None:
        try:
            with anyio.CancelScope() as request_scope, root_scope:
                request_scopes.append(request_scope)
                _ = await _coordinator(attempts, adapter, clock, fail_stop).execute(
                    request_id="cancelled-body-read",
                    body=b"{}",
                )
        except (ResponseRetirementUnresolvedError, anyio.get_cancelled_exc_class()) as error:
            observed.append(type(error))

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(execute)
        await response.read_started.wait()
        request_scopes[0].cancel()

    expected = ["reserve", "network:POST"]
    if polled:
        expected.extend(("close:origin", "network:GET"))
    expected.extend(("read:start", "close:start", "finalize", "ready:false", "root:cancel"))
    assert attempts.events == expected
    assert observed == [ResponseRetirementUnresolvedError]
    assert response.close_count == 1
    assert attempts.terminals[0].status_class is LastStatusClass.CANCELLED
    assert fail_stop.triggered is True

    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()
    assert attempts.events == [*expected, "close:finish"]


@pytest.mark.parametrize(
    ("primary", "cleanup"),
    [
        pytest.param(
            PinnedRuntimeDriftError(),
            PinnedTransportDriftError(),
            id="runtime_then_transport",
        ),
        pytest.param(
            PinnedTransportDriftError(),
            PinnedRuntimeDriftError(),
            id="transport_then_runtime",
        ),
        pytest.param(
            RuntimeError("synthetic read failure"),
            PinnedRuntimeDriftError(),
            id="ordinary_then_runtime",
        ),
    ],
)
@pytest.mark.parametrize("polled", [False, True], ids=("direct", "poll"))
async def test_real_adapter_cleanup_drift_cannot_precede_cancelled_terminal(
    primary: Exception,
    cleanup: Exception,
    polled: bool,
) -> None:
    attempts = _Attempts((_KEY_ONE,))
    response = _DriftReadCloseResponse(
        primary=primary,
        cleanup=cleanup,
        events=attempts.events,
    )
    responses: tuple[_Origin202Response | _DriftReadCloseResponse, ...] = (
        (_Origin202Response(attempts.events), response) if polled else (response,)
    )
    client = _DriftReadCloseClient(responses, attempts.events)
    clock = _Clock()
    root_scope = anyio.CancelScope()
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(attempts.events),
        _RootCancellation(attempts.events, root_scope),
    )
    adapter = NvidiaHostedAdapter(
        NvidiaAdapterDependencies(
            client=client,
            monotonic_clock=clock,
            wall_clock=clock,
            sleeper=_Sleeper(),
            jitter=_Jitter(),
            fail_stop=fail_stop,
        )
    )

    with pytest.raises(type(primary)) as captured, root_scope:
        _ = await _coordinator(attempts, adapter, clock, fail_stop).execute(
            request_id="real-adapter-fatal-drift",
            body=b"{}",
        )

    assert captured.value is primary
    assert response.close_count == 1
    expected = ["reserve", "network:POST"]
    if polled:
        expected.extend(("close:origin", "network:GET"))
    expected.extend(("read:drift", "close:drift", "finalize", "ready:false", "root:cancel"))
    assert attempts.events == expected
    assert attempts.terminals[0].status_class is LastStatusClass.CANCELLED
    assert fail_stop.triggered is True


async def test_maximum_two_failures_use_fixed_aggregation_precedence() -> None:
    attempts = _Attempts((_KEY_ONE, _KEY_TWO))
    adapter = _Adapter((_failure(408), _failure(401)), attempts.events)
    clock = _Clock()

    result = await _coordinator(attempts, adapter, clock).execute(
        request_id="request-2",
        body=b"{}",
    )

    assert isinstance(result, RoutedFailure)
    assert result.attempt_count == 2
    assert result.terminal.outcome.code == "upstream_timeout"
    assert tuple(observation.key_id for observation in result.attempt_observations) == (
        _KEY_ONE,
        _KEY_TWO,
    )
    assert tuple(observation.safe_status_class for observation in result.attempt_observations) == (
        LastStatusClass.TIMEOUT,
        LastStatusClass.INVALID_CREDENTIAL,
    )
    assert all(
        observation.terminal_outcome is TerminalOutcome.FAILED
        for observation in result.attempt_observations
    )
    assert len(attempts.terminals) == 2


async def test_stream_hands_off_live_lease_without_premature_success_persistence() -> None:
    attempts = _Attempts((_KEY_ONE,))
    stream_terminal = StreamTerminal(
        stream=_stream(),
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.SSE,
            content_length=None,
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"text/event-stream"),),
        ),
    )
    adapter = _Adapter((stream_terminal,), attempts.events)
    clock = _Clock()

    result = await _coordinator(attempts, adapter, clock).execute(
        request_id="request-3",
        body=b"{}",
    )

    assert isinstance(result, RoutedStream)
    assert result.lease.key_id == _KEY_ONE
    assert result.attempt_observations == ()
    assert attempts.terminals == []


async def test_initial_no_eligible_key_performs_no_network_or_terminal_write() -> None:
    attempts = _Attempts(())
    adapter = _Adapter((), attempts.events)
    clock = _Clock()

    result = await _coordinator(attempts, adapter, clock).execute(
        request_id="request-4",
        body=b"{}",
    )

    assert isinstance(result, RoutedFailure)
    assert result.terminal.outcome == map_public_outcome(NoEligibleKey())
    assert result.attempt_count == 0
    assert adapter.credentials == []
    assert attempts.terminals == []


async def test_stream_conversion_is_validated_before_success_is_committed() -> None:
    attempts = _Attempts((_KEY_ONE,))
    adapter = _Adapter((_json(),), attempts.events)

    result = await _coordinator(attempts, adapter, _Clock()).execute(
        request_id="request-invalid-conversion",
        body=b"{}",
        requested_stream=True,
    )

    assert isinstance(result, RoutedFailure)
    assert result.terminal.outcome.code == "upstream_protocol_error"
    assert len(attempts.terminals) == 1
    assert attempts.terminals[0].status_class is LastStatusClass.UPSTREAM_PROTOCOL_ERROR


async def test_stream_conversion_frames_are_frozen_before_success_commit() -> None:
    attempts = _Attempts((_KEY_ONE,))
    adapter = _Adapter((_stream_convertible_json(),), attempts.events)

    result = await _coordinator(attempts, adapter, _Clock()).execute(
        request_id="request-valid-conversion",
        body=b"{}",
        requested_stream=True,
    )

    assert isinstance(result, RoutedJson)
    assert result.stream_frames is not None
    assert result.stream_frames[0].startswith(b"data: ")
    assert result.stream_frames[1] == b"data: [DONE]\n\n"
    assert attempts.terminals[0].status_class is LastStatusClass.SUCCESS


async def test_second_reservation_diagnostic_never_replaces_first_terminal() -> None:
    attempts = _ClassifiedNoEligibleAttempts(
        (_KEY_ONE,),
        NoEligibleReason.RATE_COOLDOWN,
        retry_after_seconds=10,
    )
    adapter = _Adapter((_failure(401),), attempts.events)

    result = await _coordinator(attempts, adapter, _Clock()).execute(
        request_id="first-terminal-wins",
        body=b"{}",
    )

    assert isinstance(result, RoutedFailure)
    assert result.attempt_count == 1
    assert result.terminal.outcome.code == "upstream_auth_error"
    assert len(adapter.credentials) == 1


async def test_second_poll_timeout_is_exact_terminal_not_timeout_aggregate_member() -> None:
    attempts = _Attempts((_KEY_ONE, _KEY_TWO))
    poll_timeout = FailureTerminal(map_public_outcome(PollDeadline()), None)
    adapter = _Adapter((_failure(429, retry_after=5), poll_timeout), attempts.events)

    result = await _coordinator(attempts, adapter, _Clock()).execute(
        request_id="poll-timeout-exact",
        body=b"{}",
    )

    assert isinstance(result, RoutedFailure)
    assert result.attempt_count == 2
    assert result.terminal.outcome.code == "poll_timeout"


async def test_two_rate_limits_select_earliest_retry_after() -> None:
    attempts = _Attempts((_KEY_ONE, _KEY_TWO))
    adapter = _Adapter(
        (_failure(429, retry_after=300), _failure(429, retry_after=1)),
        attempts.events,
    )

    result = await _coordinator(attempts, adapter, _Clock()).execute(
        request_id="earliest-rate-retry",
        body=b"{}",
    )

    assert isinstance(result, RoutedFailure)
    assert result.terminal.outcome.code == "upstream_rate_limited"
    assert result.terminal.retry_after_seconds == 1


@pytest.mark.parametrize(
    ("reason", "retry_after", "code"),
    [
        (NoEligibleReason.RATE_COOLDOWN, 12, "upstream_rate_limited"),
        (NoEligibleReason.TRANSIENT_COOLDOWN, None, "upstream_unavailable"),
    ],
)
async def test_initial_active_cooldown_has_exact_public_classification(
    reason: NoEligibleReason,
    retry_after: int | None,
    code: str,
) -> None:
    attempts = _ClassifiedNoEligibleAttempts((), reason, retry_after)
    adapter = _Adapter((), attempts.events)

    result = await _coordinator(attempts, adapter, _Clock()).execute(
        request_id="initial-cooldown",
        body=b"{}",
    )

    assert isinstance(result, RoutedFailure)
    assert result.attempt_count == 0
    assert result.terminal.outcome.code == code
    assert result.terminal.retry_after_seconds == retry_after
    assert adapter.credentials == []


@pytest.mark.parametrize(
    ("attempts", "status", "expected_seconds"),
    [
        (_StreakAttempts(rate_streak=2), 429, 8),
        (_StreakAttempts(transient_streak=3), 503, 8),
    ],
)
async def test_cooldown_backoff_uses_lease_time_streak_snapshot(
    attempts: _StreakAttempts,
    status: int,
    expected_seconds: int,
) -> None:
    adapter = _Adapter((_failure(status),), attempts.events)

    result = await _coordinator(attempts, adapter, _Clock()).execute(
        request_id=f"streak-{status}",
        body=b"{}",
        explicit_probe_key_id=_KEY_ONE,
    )

    assert isinstance(result, RoutedFailure)
    assert attempts.terminals[0].cooldown_until == datetime(
        2026, 7, 13, 0, 0, expected_seconds, tzinfo=UTC
    )


def test_attempt_lease_credential_is_nonrepr_and_not_json_serializable() -> None:
    command = AttemptStartCommand(
        started_event_id=_KEY_ONE,
        terminal_event_id=_KEY_TWO,
        request_id="lease-custody",
        service_epoch=_EPOCH,
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset(),
        started_at=datetime(2026, 7, 13, tzinfo=UTC),
    )
    lease = AttemptLease(command.identity, _KEY_ONE, SecretStr("literal-secret"))

    rendered = repr(lease)
    assert "credential" not in rendered
    assert "SecretStr" not in rendered
    assert "literal-secret" not in rendered
    with pytest.raises(TypeError):
        _ = json.dumps(lease)


async def test_pre_handoff_adapter_cancellation_persists_cancelled_then_reraises() -> None:
    attempts = _Attempts((_KEY_ONE,))
    adapter = _BlockingAdapter()
    clock = _Clock()
    scopes: list[anyio.CancelScope] = []

    async def execute() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            _ = await RoutingCoordinator(
                RoutingCoordinatorDependencies(
                    attempts=attempts,
                    adapter=adapter,
                    clock=clock,
                    monotonic_clock=clock,
                    uuid_source=_UuidSource(),
                    service_epoch=_EPOCH,
                    jitter=_Jitter(),
                    fail_stop=_FailStop(),
                )
            ).execute(request_id="cancel-adapter", body=b"{}")

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(execute)
        await adapter.started.wait()
        scopes[0].cancel()

    assert len(attempts.terminals) == 1
    assert attempts.terminals[0].outcome is TerminalOutcome.CANCELLED
    assert attempts.terminals[0].status_class is LastStatusClass.CANCELLED


async def test_cancellation_after_stream_prime_retires_before_handoff() -> None:
    attempts = _Attempts((_KEY_ONE,))
    response = _StreamResponse()
    terminal = StreamTerminal(
        stream=_stream(response),
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.SSE,
            content_length=None,
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"text/event-stream"),),
        ),
    )
    adapter = _CancelOnReturnAdapter(terminal, attempts.events)
    with anyio.CancelScope() as scope:
        adapter.scope = scope
        _ = await _coordinator(attempts, adapter, _Clock()).execute(
            request_id="cancel-after-prime",
            body=b"{}",
        )

    assert response.close_count == 1
    assert len(attempts.terminals) == 1
    assert attempts.terminals[0].outcome is TerminalOutcome.CANCELLED


async def test_pre_handoff_cancellation_unresolved_retirement_triggers_fail_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        response_retirement_module,
        "_RESPONSE_RETIRE_TIMEOUT_SECONDS",
        0.01,
    )
    attempts = _Attempts((_KEY_ONE,))
    response = _UnresolvedCloseStreamResponse()
    fail_stop = _FailStop()
    terminal = StreamTerminal(
        stream=_stream(response, fail_stop),
        headers=ValidatedUpstreamHeaders(
            media_type=MediaType.SSE,
            content_length=None,
            retry_after_seconds=None,
            request_id=None,
            application_headers=((b"Content-Type", b"text/event-stream"),),
        ),
    )
    adapter = _CancelOnReturnAdapter(terminal, attempts.events)

    with anyio.CancelScope() as scope:
        adapter.scope = scope
        _ = await _coordinator(attempts, adapter, _Clock()).execute(
            request_id="cancel-after-prime-unresolved",
            body=b"{}",
        )

    assert response.close_started.is_set()
    assert not response.close_finished.is_set()
    assert fail_stop.calls == 1
    assert len(attempts.terminals) == 1
    assert attempts.terminals[0].outcome is TerminalOutcome.CANCELLED
    response.release.set()
    with anyio.fail_after(1):
        await response.close_finished.wait()


async def test_cancellation_during_success_commit_finishes_success_without_reclassification() -> (
    None
):
    attempts = _BlockingFinalizeAttempts()
    adapter = _Adapter((_json(),), attempts.events)
    clock = _Clock()
    scopes: list[anyio.CancelScope] = []

    async def execute() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            _ = await _coordinator(attempts, adapter, clock).execute(
                request_id="cancel-success-commit",
                body=b"{}",
            )

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(execute)
        await attempts.finalize_started.wait()
        scopes[0].cancel()
        attempts.release_finalize.set()

    assert len(attempts.terminals) == 1
    assert attempts.terminals[0].outcome is TerminalOutcome.SUCCEEDED
    assert attempts.terminals[0].status_class is LastStatusClass.SUCCESS
