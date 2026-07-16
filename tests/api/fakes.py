"""Typed Todo 5 fakes, including the real router over an ASGI NVIDIA fake."""

from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from uuid import UUID

from pydantic import SecretStr

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.api_streaming import LoggedResponderFactory
from nvidia_build_lb.api_types import ApplicationServices
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptLease,
    AttemptStartCommand,
    TerminalCommitted,
)
from nvidia_build_lb.config import DeploymentStage, LogLevel
from nvidia_build_lb.logging import LoggingConfig, ServiceLogger, init_logging
from nvidia_build_lb.nvidia_adapter import NvidiaAdapterDependencies, NvidiaHostedAdapter
from nvidia_build_lb.outcome_types import SourceSignal
from nvidia_build_lb.outcomes import (
    LedgerCapacityExhausted,
    NoEligibleKey,
    ReservationFailure,
    map_public_outcome,
)
from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.routing import (
    RoutedFailure,
    RoutedResult,
    RoutingCoordinator,
    RoutingCoordinatorDependencies,
)
from nvidia_build_lb.routing_models import RoutedAttemptObservation
from nvidia_build_lb.scheduler_state import NoEligibleUpstreamKeyError, TerminalOutcome
from nvidia_build_lb.transport_adapter import SanitizedAsyncClient
from tests.contracts.fakes import FakeReadiness, FakeRouter, contract_services
from tests.harness.fake_nvidia import FakeNvidiaHarness, ScriptedResponse

from .typing_helpers import BodyRecord

_KEY_ID = UUID("00000000-0000-4000-8000-000000000001")
_SERVICE_EPOCH = UUID("00000000-0000-4000-8000-000000000100")
_SYNTHETIC_CREDENTIAL = SecretStr("nvapi-synthetic-api-upstream")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _logger(stream: StringIO, name: str) -> ServiceLogger:
    return init_logging(
        LoggingConfig(
            name=name,
            stage=DeploymentStage.PRODUCTION,
            level=LogLevel.INFO,
            stream=stream,
        )
    )


class ScenarioRouter:
    """Record exact forwarded JSON and optionally return one safe failure."""

    bodies: list[BodyRecord]
    request_ids: list[str]
    _failure: tuple[SourceSignal, int | None] | None
    _normal: FakeRouter

    def __init__(self) -> None:
        self.bodies = []
        self.request_ids = []
        self._failure = None
        self._normal = FakeRouter()

    def fail_with(self, signal: SourceSignal, retry_after_seconds: int | None = None) -> None:
        """Make the next routed call return one closed safe outcome."""
        self._failure = (signal, retry_after_seconds)

    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedResult:
        """Record the public inputs and delegate or consume the next failure."""
        self.bodies.append(BodyRecord(body))
        self.request_ids.append(request_id)
        failure = self._failure
        self._failure = None
        if failure is not None:
            signal, retry_after_seconds = failure
            outcome = map_public_outcome(signal)
            if isinstance(signal, (LedgerCapacityExhausted, NoEligibleKey, ReservationFailure)):
                attempt_count = 0
                observations: tuple[RoutedAttemptObservation, ...] = ()
            else:
                attempt_count = 1
                observations = (
                    RoutedAttemptObservation(
                        key_id=_KEY_ID,
                        attempt_ordinal=1,
                        safe_status_class=(
                            outcome.persisted_status or LastStatusClass.UPSTREAM_PROTOCOL_ERROR
                        ),
                        terminal_outcome=TerminalOutcome.FAILED,
                    ),
                )
            return RoutedFailure(
                FailureTerminal(outcome, retry_after_seconds),
                attempt_count,
                observations,
            )
        return await self._normal.execute(
            request_id=request_id,
            body=body,
            requested_stream=requested_stream,
            explicit_probe_key_id=explicit_probe_key_id,
        )


@dataclass(frozen=True, slots=True)
class ApiHarness:
    """Function-scoped production composition with observable safe seams."""

    services: ApplicationServices
    router: ScenarioRouter
    readiness: FakeReadiness
    log_stream: StringIO


def build_api_harness() -> ApiHarness:
    """Build the composed API over deterministic in-process service fakes."""
    base, readiness = contract_services()
    router = ScenarioRouter()
    stream = StringIO()
    services = ApplicationServices(
        credentials=base.credentials,
        routing=router,
        responders=base.responders,
        readiness=readiness,
        logger=_logger(stream, "nvidia-build-lb.api-tests"),
    )
    return ApiHarness(services, router, readiness, stream)


class _Clock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 1.0


class _Sleeper:
    async def sleep(self, seconds: float) -> None:
        del seconds


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


class _UuidSource:
    _next: int

    def __init__(self) -> None:
        self._next = 1000

    def new(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)


class MemoryAttemptStore:
    """Minimal durable-contract fake used only around the real routing pipeline."""

    starts: list[AttemptStartCommand]
    terminals: list[AttemptFinalizeCommand]

    def __init__(self) -> None:
        self.starts = []
        self.terminals = []

    async def reserve_attempt(self, command: AttemptStartCommand) -> AttemptLease:
        """Return the one synthetic key unless the coordinator excluded it."""
        if _KEY_ID in command.excluded_key_ids:
            raise NoEligibleUpstreamKeyError
        self.starts.append(command)
        return AttemptLease(command.identity, _KEY_ID, _SYNTHETIC_CREDENTIAL)

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        """Record one terminal receipt and return its exact identity."""
        self.terminals.append(command)
        return TerminalCommitted(command.identity)


@dataclass(frozen=True, slots=True)
class LiveFakeUpstreamHarness:
    """Real API/routing/adapter stack terminating at a deterministic ASGI upstream."""

    services: ApplicationServices
    upstream: FakeNvidiaHarness
    client: SanitizedAsyncClient
    attempts: MemoryAttemptStore
    log_stream: StringIO
    fail_stop: _FailStop

    async def close(self) -> None:
        """Close the one shared pinned client after application requests drain."""
        await self.client.aclose()


def _chat_json(identifier: str) -> ScriptedResponse:
    return ScriptedResponse.json(
        {
            "id": identifier,
            "object": "chat.completion",
            "created": 1_767_225_600,
            "model": "z-ai/glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "fake upstream ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }
    )


def _chat_stream(identifier: str) -> ScriptedResponse:
    prefix = (
        f'data: {{"id":"{identifier}","object":"chat.completion.chunk",'
        '"created":1767225600,"model":"z-ai/glm-5.2","choices":[{"index":0,'
    )
    return ScriptedResponse.sse(
        (
            prefix + '"delta":{"role":"assistant","content":"fake "},"finish_reason":null}]}\n\n'
        ).encode(),
        (prefix + '"delta":{"content":"upstream ok"},"finish_reason":"stop"}]}\n\n').encode(),
        b"data: [DONE]\n\n",
    )


def _chat_midstream_failure() -> ScriptedResponse:
    frame = b"".join(
        (
            b'data: {"id":"midstream","choices":[{"index":0,',
            b'"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
        )
    )
    return ScriptedResponse.sse(frame)


def build_live_fake_upstream_harness() -> LiveFakeUpstreamHarness:
    """Build the live-client stack with success, stream, and error terminals."""
    upstream = FakeNvidiaHarness(
        (
            _chat_json("curl-json"),
            _chat_stream("curl-stream"),
            _chat_json("sdk-json"),
            _chat_stream("sdk-stream"),
            _chat_midstream_failure(),
            ScriptedResponse.text(
                "provider-json-error-body-must-not-escape",
                status_code=400,
                media_type="application/json",
            ),
            ScriptedResponse.text(
                "provider-problem-json-body-must-not-escape",
                status_code=451,
                media_type="application/problem+json",
            ),
            ScriptedResponse.text(
                "provider-text-error-body-must-not-escape",
                status_code=429,
                media_type="text/plain",
            ),
        )
    )
    client = SanitizedAsyncClient.from_transport_for_test(httpx2.ASGITransport(app=upstream.app))
    clock = _Clock()
    fail_stop = _FailStop()
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
    attempts = MemoryAttemptStore()
    router = RoutingCoordinator(
        RoutingCoordinatorDependencies(
            attempts=attempts,
            adapter=adapter,
            clock=clock,
            monotonic_clock=clock,
            uuid_source=_UuidSource(),
            service_epoch=_SERVICE_EPOCH,
            jitter=_Jitter(),
            fail_stop=fail_stop,
        )
    )
    base, readiness = contract_services()
    stream = StringIO()
    logger = _logger(stream, "nvidia-build-lb.api-live-tests")
    services = ApplicationServices(
        credentials=base.credentials,
        routing=router,
        responders=LoggedResponderFactory(attempts, clock, clock, logger),
        readiness=readiness,
        logger=logger,
    )
    return LiveFakeUpstreamHarness(services, upstream, client, attempts, stream, fail_stop)


def successful_terminals(harness: LiveFakeUpstreamHarness, *, count: int) -> bool:
    """Return whether the requested prefix contains successful attempts."""
    terminals = harness.attempts.terminals[:count]
    return len(terminals) == count and all(
        terminal.outcome is TerminalOutcome.SUCCEEDED
        and terminal.status_class is LastStatusClass.SUCCESS
        for terminal in terminals
    )
