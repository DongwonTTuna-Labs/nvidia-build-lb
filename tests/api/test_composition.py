"""Production API behavior, safe mapping, correlation, and redaction tests."""

from dataclasses import dataclass, replace

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from nvidia_build_lb.admin.schemas import (
    AdminOperatorReadinessRead,
    AdminOverviewRead,
    LastStatusClass,
    OverviewStatus,
)
from nvidia_build_lb.api_types import RepositoryReadinessProbe
from nvidia_build_lb.logging import StructuredLogLine
from nvidia_build_lb.main import create_app
from nvidia_build_lb.outcome_types import SourceSignal
from nvidia_build_lb.outcomes import (
    HttpStatusSignal,
    NoEligibleKey,
    PollDeadline,
    ProtocolFailure,
    ReservationFailure,
    TransportErrorCode,
    TransportSignal,
    map_public_outcome,
)
from nvidia_build_lb.scheduler_state import TerminalOutcome
from tests.contracts._support import (
    ADMIN_TOKEN,
    CHAT_TOKEN,
    ENABLED_KEY_ID,
    MODELS_TOKEN,
    REVOKED_TOKEN,
    ContractClient,
    assert_error,
    assert_security_headers,
    bearer,
)
from tests.contracts.fakes import (
    FakeCredentialRepositories,
    FakeDownstreamRepository,
    FakeUpstreamRepository,
)

from .fakes import ApiHarness

pytestmark = pytest.mark.api


@dataclass(frozen=True, slots=True)
class _AuthCase:
    method: str
    path: str
    token: str | None
    status: int
    code: str


@dataclass(frozen=True, slots=True)
class _FailureCase:
    signal: SourceSignal
    status: int
    code: str
    retry_after: int | None = None
    keyed: bool = True


def test_health_and_static_admin_are_composed(api_client: ContractClient) -> None:
    api_client.set_ready(True)
    assert api_client.request("GET", "/health").content == b'{"status":"ok","ready":true}'
    api_client.set_ready(False)
    degraded = api_client.request("GET", "/health")
    assert degraded.status_code == 503
    assert degraded.content == b'{"status":"degraded","ready":false}'
    assert api_client.request("GET", "/admin").status_code == 200
    admin = api_client.request(
        "GET",
        "/admin/api/v1/overview",
        headers=bearer(ADMIN_TOKEN),
    )
    assert admin.status_code == 200


def test_operator_readiness_is_one_authenticated_closed_read(
    api_client: ContractClient,
) -> None:
    path = "/admin/api/v1/operator-readiness"

    missing = api_client.request("GET", path)
    response = api_client.request("GET", path, headers=bearer(ADMIN_TOKEN))
    query = api_client.request("GET", f"{path}?expand=true", headers=bearer(ADMIN_TOKEN))
    wrong_method = api_client.request("POST", path, headers=bearer(ADMIN_TOKEN))

    readiness = AdminOperatorReadinessRead.model_validate_json(response.content)
    assert missing.status_code == 401
    assert response.status_code == 200
    assert readiness.runtime_state.value == "operational"
    assert query.status_code == 422
    assert wrong_method.status_code == 405
    assert wrong_method.headers["allow"] == "GET"


@pytest.mark.parametrize("ready", [True, False], ids=("ready", "degraded"))
def test_health_and_admin_overview_share_readiness_state(
    api_client: ContractClient,
    ready: bool,
) -> None:
    api_client.set_ready(ready)

    health = api_client.request("GET", "/health")
    overview_response = api_client.request(
        "GET",
        "/admin/api/v1/overview",
        headers=bearer(ADMIN_TOKEN),
    )
    overview = AdminOverviewRead.model_validate_json(overview_response.content)

    assert health.status_code == (200 if ready else 503)
    assert overview_response.status_code == 200
    assert overview.ready is ready
    assert overview.status is (OverviewStatus.OK if ready else OverviewStatus.DEGRADED)


def test_repository_readiness_probe_uses_admin_overview_repository(
    api_harness: ApiHarness,
) -> None:
    probe = RepositoryReadinessProbe(api_harness.services.credentials.repositories)

    api_harness.readiness.set_ready(False)
    assert anyio.run(probe.is_ready) is False
    api_harness.readiness.set_ready(True)
    assert anyio.run(probe.is_ready) is True


@pytest.mark.parametrize(
    "failure",
    [pytest.param(OSError(), id="connect"), pytest.param(SQLAlchemyError(), id="sqlalchemy")],
)
def test_health_database_failure_is_exact_minimal_degraded_body(
    api_harness: ApiHarness,
    monkeypatch: pytest.MonkeyPatch,
    failure: OSError | SQLAlchemyError,
) -> None:
    repositories = api_harness.services.credentials.repositories

    async def unavailable(_repositories: object) -> AdminOverviewRead:
        raise failure

    monkeypatch.setattr(type(repositories), "overview", unavailable)
    services = replace(
        api_harness.services,
        readiness=RepositoryReadinessProbe(repositories),
    )

    with TestClient(
        create_app(services),
        base_url="http://127.0.0.1:2456",
    ) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.content == b'{"status":"degraded","ready":false}'


@pytest.mark.parametrize(
    ("path", "owner", "method"),
    [
        ("/admin/api/v1/dashboard", FakeCredentialRepositories, "dashboard"),
        (
            "/admin/api/v1/operator-readiness",
            FakeCredentialRepositories,
            "operator_readiness",
        ),
        ("/admin/api/v1/overview", FakeCredentialRepositories, "overview"),
        ("/admin/api/v1/events", FakeCredentialRepositories, "events"),
        ("/admin/api/v1/upstream-keys", FakeUpstreamRepository, "list_all"),
        ("/admin/api/v1/downstream-tokens", FakeDownstreamRepository, "list_all"),
    ],
)
def test_every_supported_admin_read_has_the_same_server_deadline(
    api_harness: ApiHarness,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    owner: type[object],
    method: str,
) -> None:
    async def never_returns(*_args: object) -> object:
        await anyio.sleep_forever()
        raise AssertionError

    monkeypatch.setattr(owner, method, never_returns)
    credentials = replace(
        api_harness.services.credentials,
        admin_read_deadline_seconds=0.01,
    )
    services = replace(api_harness.services, credentials=credentials)

    with TestClient(create_app(services), base_url="http://127.0.0.1:2456") as client:
        response = client.get(path, headers=bearer(ADMIN_TOKEN))

    assert_error(response, status_code=504, code="admin_read_timeout")


def test_health_converges_to_bounded_degraded_when_repository_read_stalls(
    api_harness: ApiHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = api_harness.services.credentials.repositories

    async def never_returns(_repositories: object) -> AdminOverviewRead:
        await anyio.sleep_forever()
        raise AssertionError

    monkeypatch.setattr(type(repositories), "overview", never_returns)
    services = replace(
        api_harness.services,
        readiness=RepositoryReadinessProbe(repositories, deadline_seconds=0.01),
    )

    with TestClient(create_app(services), base_url="http://127.0.0.1:2456") as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.content == b'{"status":"degraded","ready":false}'


def test_unhandled_admin_error_is_safe_and_has_security_headers(
    api_harness: ApiHarness,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = api_harness.services.credentials.repositories
    sentinel = "exception-message-must-never-enter-server-log"

    async def unexpected(_repositories: object) -> AdminOverviewRead:
        raise RuntimeError(sentinel)

    monkeypatch.setattr(type(repositories), "overview", unexpected)

    with TestClient(
        create_app(api_harness.services),
        base_url="http://127.0.0.1:2456",
    ) as client:
        response = client.get(
            "/admin/api/v1/overview",
            headers=bearer(ADMIN_TOKEN),
        )

    assert_error(response, status_code=500, code="internal_server_error")
    assert_security_headers(response)
    assert sentinel not in response.text
    assert sentinel not in caplog.text


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(_AuthCase("GET", "/v1/models", None, 401, "unauthorized"), id="missing"),
        pytest.param(
            _AuthCase("GET", "/v1/models", ADMIN_TOKEN, 401, "unauthorized"),
            id="wrong-realm",
        ),
        pytest.param(
            _AuthCase("GET", "/v1/models", REVOKED_TOKEN, 401, "unauthorized"),
            id="revoked",
        ),
        pytest.param(
            _AuthCase("GET", "/v1/models", CHAT_TOKEN, 403, "insufficient_scope"),
            id="models-scope",
        ),
        pytest.param(
            _AuthCase(
                "POST",
                "/v1/chat/completions",
                MODELS_TOKEN,
                403,
                "insufficient_scope",
            ),
            id="chat-scope",
        ),
    ],
)
def test_public_auth_scope_matrix(api_client: ContractClient, case: _AuthCase) -> None:
    headers = {} if case.token is None else bearer(case.token)
    response = api_client.request(case.method, case.path, headers=headers)
    assert_error(response, status_code=case.status, code=case.code)


def test_chat_preserves_extensions_and_emits_correlated_safe_log(
    api_client: ContractClient,
    api_harness: ApiHarness,
) -> None:
    message_sentinel = "message-body-must-never-enter-log"
    client_request_id = "client-request-id-must-be-ignored"
    response = api_client.request(
        "POST",
        "/v1/chat/completions",
        headers={**bearer(CHAT_TOKEN), "X-Request-ID": client_request_id},
        json_body={
            "model": "z-ai/glm-5.2",
            "messages": [
                {
                    "role": "user",
                    "content": message_sentinel,
                    "nvidia_message_extension": {"preserved": True},
                }
            ],
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )

    assert response.status_code == 200
    forwarded = api_harness.router.bodies[-1].raw
    assert b'"chat_template_kwargs":{"enable_thinking":false}' in forwarded
    assert b'"nvidia_message_extension":{"preserved":true}' in forwarded
    lines = api_harness.log_stream.getvalue().splitlines()
    assert len(lines) == 1
    event = StructuredLogLine.model_validate_json(lines[0])
    assert event.request_id == api_harness.router.request_ids[-1]
    assert event.request_id != client_request_id
    assert str(event.internal_key_id) == ENABLED_KEY_ID
    assert event.attempt_ordinal == 1
    assert event.safe_status_class is LastStatusClass.SUCCESS
    assert event.terminal_outcome is TerminalOutcome.SUCCEEDED
    corpus = api_harness.log_stream.getvalue().lower()
    for forbidden in (message_sentinel, CHAT_TOKEN, client_request_id, "authorization"):
        assert forbidden.lower() not in corpus


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _FailureCase(NoEligibleKey(), 503, "no_upstream_keys", keyed=False),
            id="no-key",
        ),
        pytest.param(
            _FailureCase(ReservationFailure(), 503, "database_unavailable", keyed=False),
            id="database",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(401), 502, "upstream_auth_error"),
            id="upstream-401",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(402), 503, "upstream_credits_exhausted"),
            id="upstream-402",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(408), 504, "upstream_timeout"),
            id="upstream-408",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(422), 422, "upstream_request_rejected"),
            id="upstream-422",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(429), 429, "upstream_rate_limited", 7),
            id="upstream-429",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(500), 502, "upstream_internal_error"),
            id="upstream-500",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(502), 502, "upstream_bad_gateway"),
            id="upstream-502",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(503), 503, "upstream_unavailable"),
            id="upstream-503",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(504), 504, "upstream_timeout"),
            id="upstream-504",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(418), 418, "upstream_request_rejected"),
            id="upstream-418",
        ),
        pytest.param(
            _FailureCase(HttpStatusSignal(451), 451, "upstream_request_rejected"),
            id="upstream-451",
        ),
        pytest.param(
            _FailureCase(ProtocolFailure(), 502, "upstream_protocol_error"),
            id="protocol",
        ),
        pytest.param(_FailureCase(PollDeadline(), 504, "poll_timeout"), id="poll-timeout"),
        pytest.param(
            _FailureCase(
                TransportSignal(TransportErrorCode.CONNECT_ERROR, 0),
                503,
                "upstream_unavailable",
            ),
            id="connect",
        ),
        pytest.param(
            _FailureCase(
                TransportSignal(TransportErrorCode.READ_TIMEOUT, 1),
                504,
                "upstream_timeout",
            ),
            id="read-timeout",
        ),
    ],
)
def test_routed_failures_use_only_locked_safe_mapping(
    api_client: ContractClient,
    api_harness: ApiHarness,
    case: _FailureCase,
) -> None:
    api_harness.router.fail_with(case.signal, case.retry_after)
    response = api_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body={
            "model": "z-ai/glm-5.2",
            "messages": [{"role": "user", "content": "safe failure probe"}],
        },
    )

    assert_error(response, status_code=case.status, code=case.code)
    assert response.headers.get("retry-after") == (
        None if case.retry_after is None else str(case.retry_after)
    )
    event = StructuredLogLine.model_validate_json(api_harness.log_stream.getvalue())
    assert event.terminal_outcome is TerminalOutcome.FAILED
    assert event.request_id == api_harness.router.request_ids[-1]
    assert (event.internal_key_id is not None) is case.keyed
    if case.keyed:
        assert str(event.internal_key_id) == ENABLED_KEY_ID
        assert event.attempt_ordinal == 1
        assert event.safe_status_class is map_public_outcome(case.signal).persisted_status
