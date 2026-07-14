"""Deterministic injected services for closed HTTP boundary contracts."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import StringIO
from uuid import UUID

from pydantic import SecretStr

from nvidia_build_lb.admin.schemas import (
    AdminEventListResponse,
    AdminEventRead,
    AdminOverviewRead,
    DownstreamScope,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    DownstreamTokenOverview,
    DownstreamTokenRead,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    OverviewStatus,
    ProbeStatus,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyOverview,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)
from nvidia_build_lb.admin_credentials import CredentialServices
from nvidia_build_lb.api_types import ApplicationServices, StreamLogContext
from nvidia_build_lb.auth import (
    AdminAuthenticator,
    CredentialAuthenticators,
    DownstreamAuthenticator,
)
from nvidia_build_lb.config import DeploymentStage, LogLevel
from nvidia_build_lb.credential_types import (
    AuthenticationRejectedError,
    AuthRealm,
    DownstreamPrincipal,
    InsufficientScopeError,
)
from nvidia_build_lb.header_types import MediaType, ValidatedUpstreamHeaders
from nvidia_build_lb.logging import LoggingConfig, init_logging
from nvidia_build_lb.outcomes import HttpStatusSignal, map_public_outcome
from nvidia_build_lb.polling import FailureTerminal, JsonTerminal
from nvidia_build_lb.representations import read_json_representation
from nvidia_build_lb.routing import (
    RoutedFailure,
    RoutedJson,
    RoutedResult,
)
from nvidia_build_lb.routing_models import RoutedAttemptObservation
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.streaming import ChatStreamResponder

from ._support import (
    ADMIN_TOKEN,
    CHAT_TOKEN,
    DISABLED_KEY_ID,
    DOWNSTREAM_ID,
    ENABLED_KEY_ID,
    MODELS_TOKEN,
    REVOKED_TOKEN,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_CHAT_JSON = (
    b'{"id":"contract-chat","object":"chat.completion","model":"z-ai/glm-5.2",'
    b'"choices":[{"index":0,"message":{"role":"assistant","content":"ok"}}]}'
)
_CHAT_FRAME = b'data: {"id":"contract-chat","choices":[{"delta":{"content":"ok"}}]}\n\n'
_DONE_FRAME = b"data: [DONE]\n\n"
_ERROR_FRAME = (
    b'event: error\ndata: {"error":{"code":"upstream_stream_error",'
    b'"message":"upstream stream ended unexpectedly",'
    b'"request_id":"contract-request"}}\n\n'
)


def _upstream(key_id: str, *, enabled: bool) -> UpstreamKeyRead:
    return UpstreamKeyRead(
        id=UUID(key_id),
        fingerprint=f"sha256:{key_id.replace('-', '') * 2}",
        enabled=enabled,
        health_state=HealthState.UNKNOWN,
        cooldown_until=None,
        request_count=0,
        success_count=0,
        failure_count=0,
        last_status_class=None,
        last_used_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


class FakeUpstreamRepository:
    async def list_all(self) -> UpstreamKeyListResponse:
        return UpstreamKeyListResponse(
            items=(
                _upstream(ENABLED_KEY_ID, enabled=True),
                _upstream(DISABLED_KEY_ID, enabled=False),
            )
        )

    async def create(
        self,
        payload: UpstreamKeyCreateRequest,
        request_id: str,
    ) -> UpstreamKeyRead:
        del request_id
        fingerprint = sha256(payload.key.encode()).hexdigest()
        created = _upstream(DISABLED_KEY_ID, enabled=False)
        return created.model_copy(update={"fingerprint": f"sha256:{fingerprint}"})

    async def enable(self, key_id: UUID, request_id: str) -> None:
        del key_id, request_id

    async def disable(self, key_id: UUID, request_id: str) -> None:
        del key_id, request_id

    async def delete(self, key_id: UUID, request_id: str) -> None:
        del key_id, request_id


class FakeDownstreamRepository:
    async def list_all(self) -> DownstreamTokenListResponse:
        return DownstreamTokenListResponse(
            items=(
                DownstreamTokenRead(
                    id=UUID(DOWNSTREAM_ID),
                    label="contract token",
                    scopes=(DownstreamScope.MODELS_READ,),
                    revoked_at=None,
                    request_count=0,
                    last_used_at=None,
                    created_at=_NOW,
                ),
            )
        )

    async def issue(
        self,
        payload: DownstreamTokenIssueRequest,
        request_id: str,
    ) -> DownstreamTokenIssued:
        del request_id
        return DownstreamTokenIssued(
            id=UUID(DOWNSTREAM_ID),
            label=payload.label,
            scopes=payload.scopes,
            token=f"nblb_ds_{'e' * 64}",
            revoked_at=None,
            request_count=0,
            last_used_at=None,
            created_at=_NOW,
        )

    async def revoke(self, token_id: UUID, request_id: str) -> None:
        del token_id, request_id

    async def authorize_digest(
        self,
        digest: bytes,
        required_scope: DownstreamScope,
    ) -> DownstreamPrincipal:
        token_scopes = {
            sha256(MODELS_TOKEN.encode()).digest(): (DownstreamScope.MODELS_READ,),
            sha256(CHAT_TOKEN.encode()).digest(): (DownstreamScope.CHAT_WRITE,),
        }
        scopes = token_scopes.get(digest)
        if scopes is None or digest == sha256(REVOKED_TOKEN.encode()).digest():
            raise AuthenticationRejectedError(AuthRealm.DOWNSTREAM)
        if required_scope not in scopes:
            raise InsufficientScopeError(required_scope)
        return DownstreamPrincipal(UUID(DOWNSTREAM_ID), scopes)


@dataclass(frozen=True, slots=True)
class FakeCredentialRepositories:
    upstream: FakeUpstreamRepository
    downstream: FakeDownstreamRepository
    readiness: "FakeReadiness"

    async def overview(self) -> AdminOverviewRead:
        ready = await self.readiness.is_ready()
        return AdminOverviewRead(
            status=OverviewStatus.OK if ready else OverviewStatus.DEGRADED,
            ready=ready,
            upstream_keys=UpstreamKeyOverview(
                total=2,
                enabled=2,
                eligible=1,
                cooling=1,
                degraded=1,
            ),
            downstream_tokens=DownstreamTokenOverview(total=3, active=2, revoked=1),
            request_count=42,
            last_event_at=_NOW,
            generated_at=_NOW + timedelta(seconds=1),
        )

    async def events(self) -> AdminEventListResponse:
        return AdminEventListResponse(
            items=tuple(
                AdminEventRead(
                    id=UUID(int=index),
                    request_id=f"event-{index}",
                    event_type=EventType.UPSTREAM_ATTEMPT,
                    upstream_key_id=UUID(ENABLED_KEY_ID),
                    downstream_token_id=None,
                    outcome_class=EventOutcome.STARTED,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=_NOW + timedelta(seconds=index),
                )
                for index in range(100, 0, -1)
            )
        )


@dataclass(frozen=True, slots=True)
class FakeProbe:
    async def probe(self, key_id: UUID, request_id: str) -> UpstreamProbeResponse:
        del request_id
        return UpstreamProbeResponse(
            id=key_id,
            enabled=False,
            probe_status=ProbeStatus.VALID,
            observed_at=_NOW,
        )


@dataclass(slots=True)
class FakeReadiness:
    ready: bool = True

    async def is_ready(self) -> bool:
        return self.ready

    def set_ready(self, ready: bool) -> None:
        self.ready = ready


class FakeRouter:
    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedResult:
        del request_id, explicit_probe_key_id
        for marker, status in (
            (b"provider-secret-body-application-json-error", 500),
            (b"provider-secret-body-problem-json-error", 503),
            (b"provider-secret-body-text-plain-error", 429),
        ):
            if marker in body:
                outcome = map_public_outcome(HttpStatusSignal(status))
                return RoutedFailure(
                    FailureTerminal(outcome, None),
                    1,
                    (
                        RoutedAttemptObservation(
                            key_id=UUID(ENABLED_KEY_ID),
                            attempt_ordinal=1,
                            safe_status_class=(
                                outcome.persisted_status or LastStatusClass.UPSTREAM_PROTOCOL_ERROR
                            ),
                            terminal_outcome=TerminalOutcome.FAILED,
                        ),
                    ),
                )
        terminal = JsonTerminal(
            read_json_representation(_CHAT_JSON),
            ValidatedUpstreamHeaders(
                MediaType.JSON,
                None,
                None,
                None,
                ((b"Content-Type", b"application/json"),),
            ),
        )
        frames = None
        if requested_stream:
            frames = (
                _CHAT_FRAME,
                _ERROR_FRAME if b"contract-midstream-failure" in body else _DONE_FRAME,
            )
        return RoutedJson(
            terminal,
            UUID(ENABLED_KEY_ID),
            1,
            frames,
            (
                RoutedAttemptObservation(
                    key_id=UUID(ENABLED_KEY_ID),
                    attempt_ordinal=1,
                    safe_status_class=LastStatusClass.SUCCESS,
                    terminal_outcome=TerminalOutcome.SUCCEEDED,
                ),
            ),
        )


class FakeResponderFactory:
    def create(self, stream_log: StreamLogContext | None = None) -> ChatStreamResponder:
        del stream_log
        return ChatStreamResponder(None)


def contract_services() -> tuple[ApplicationServices, FakeReadiness]:
    """Build a fresh deterministic dependency graph per contract test."""
    downstream = FakeDownstreamRepository()
    readiness = FakeReadiness()
    repositories = FakeCredentialRepositories(FakeUpstreamRepository(), downstream, readiness)
    credentials = CredentialServices(
        repositories,
        CredentialAuthenticators(
            AdminAuthenticator(SecretStr(ADMIN_TOKEN)),
            DownstreamAuthenticator(downstream),
        ),
        FakeProbe(),
    )
    logger = init_logging(
        LoggingConfig(
            name="nvidia-build-lb.contracts",
            stage=DeploymentStage.PRODUCTION,
            level=LogLevel.INFO,
            stream=StringIO(),
        )
    )
    return (
        ApplicationServices(credentials, FakeRouter(), FakeResponderFactory(), readiness, logger),
        readiness,
    )
