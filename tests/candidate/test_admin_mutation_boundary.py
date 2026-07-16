"""Authenticated admin mutation deadline, settlement, and response contracts."""

from dataclasses import dataclass, replace

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from nvidia_build_lb.admin.schemas import AdminDashboardRead, AdminOperatorReadinessRead
from nvidia_build_lb.admin_deadlines import AdminMutationSettlingError
from nvidia_build_lb.admin_mutation_barrier import AdminMutationBarrier, MutationBarrierSample
from nvidia_build_lb.admin_mutation_boundary import AdminMutationBoundaryMiddleware
from nvidia_build_lb.main import create_app
from nvidia_build_lb.runtime_readiness import GatedCredentialRepositories, RuntimeReadinessGate
from nvidia_build_lb.schemas import ErrorEnvelope
from tests.contracts._support import (
    ACCEPTED_HOST,
    ACCEPTED_ORIGIN,
    ADMIN_TOKEN,
    bearer,
)
from tests.contracts.fakes import FakeCredentialRepositories, contract_services

pytestmark = pytest.mark.anyio

_REQUEST_ID = "mutation-boundary-request"
_MUTATION_PATH = "/admin/api/v1/upstream-keys"


@dataclass(frozen=True, slots=True)
class _Lifecycle:
    ready: bool

    def is_ready(self) -> bool:
        """Return one fixed process-local lifecycle sample."""
        return self.ready


def _scope(path: str = _MUTATION_PATH) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "server": ("127.0.0.1", 2456),
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "method": "POST",
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "state": {"nvidia_build_lb_request_id": _REQUEST_ID},
    }


def _receive(body: bytes = b"{}") -> Receive:
    consumed = False

    async def receive() -> Message:
        nonlocal consumed
        if consumed:
            raise AssertionError
        consumed = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _status(messages: list[Message]) -> int:
    value: object = messages[0].get("status")
    assert isinstance(value, int)
    return value


def _body(messages: list[Message]) -> bytes:
    value: object = messages[-1].get("body")
    assert isinstance(value, bytes)
    return value


def _error_code(messages: list[Message]) -> str:
    return ErrorEnvelope.model_validate_json(_body(messages)).error.code


async def test_barrier_and_deadline_own_body_consumption_and_complete_response_buffer() -> None:
    barrier = AdminMutationBarrier()
    body_sample: MutationBarrierSample | None = None
    published: list[Message] = []

    async def receive() -> Message:
        nonlocal body_sample
        body_sample = barrier.sample()
        return {"type": "http.request", "body": b'{"key":"synthetic"}', "more_body": False}

    async def inner(scope: Scope, inner_receive: Receive, send: Send) -> None:
        del scope
        request = await inner_receive()
        assert request["type"] == "http.request"
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok":', "more_body": True})
        assert published == []
        await send({"type": "http.response.body", "body": b"true}", "more_body": False})
        assert published == []

    async def send(message: Message) -> None:
        assert barrier.sample().active_count == 0
        published.append(message)

    middleware = AdminMutationBoundaryMiddleware(
        inner,
        barrier=barrier,
        lifecycle=_Lifecycle(ready=True),
        deadline_seconds=5,
    )
    await middleware(_scope(), receive, send)

    assert body_sample == MutationBarrierSample(active_count=1, generation=1)
    assert barrier.sample() == MutationBarrierSample(active_count=0, generation=2)
    assert _status(published) == 201
    assert _body(published) == b'{"ok":true}'
    assert len(published) == 2


async def test_runtime_unavailable_rejects_before_body_domain_or_network_dispatch() -> None:
    barrier = AdminMutationBarrier()
    body_consumed = False
    inner_called = False
    published: list[Message] = []

    async def receive() -> Message:
        nonlocal body_consumed
        body_consumed = True
        raise AssertionError

    async def inner(scope: Scope, inner_receive: Receive, send: Send) -> None:
        del scope, inner_receive, send
        nonlocal inner_called
        inner_called = True
        raise AssertionError

    async def send(message: Message) -> None:
        published.append(message)

    middleware = AdminMutationBoundaryMiddleware(
        inner,
        barrier=barrier,
        lifecycle=_Lifecycle(ready=False),
        deadline_seconds=5,
    )
    await middleware(_scope(), receive, send)

    assert body_consumed is False
    assert inner_called is False
    assert barrier.sample() == MutationBarrierSample(active_count=0, generation=2)
    assert _status(published) == 503
    assert _error_code(published) == "runtime_unavailable"


async def test_timeout_unwinds_before_response_and_dashboard_stays_settling_until_exit() -> None:
    barrier = AdminMutationBarrier()
    lifecycle = RuntimeReadinessGate()
    lifecycle.set_ready(True)
    base_services, _ = contract_services()
    gated = GatedCredentialRepositories(
        base_services.credentials.repositories,
        lifecycle,
        barrier,
    )
    inner_started = anyio.Event()
    inner_unwound = anyio.Event()
    request_completed = anyio.Event()
    published: list[Message] = []

    async def inner(scope: Scope, inner_receive: Receive, send: Send) -> None:
        del scope, inner_receive, send
        inner_started.set()
        try:
            await anyio.sleep_forever()
        finally:
            assert barrier.sample().active_count == 1
            inner_unwound.set()

    async def send(message: Message) -> None:
        if message.get("type") == "http.response.start":
            assert inner_unwound.is_set()
            assert barrier.sample().active_count == 0
        published.append(message)

    middleware = AdminMutationBoundaryMiddleware(
        inner,
        barrier=barrier,
        lifecycle=lifecycle,
        deadline_seconds=1,
    )

    async def invoke() -> None:
        try:
            await middleware(_scope(), _receive(), send)
        finally:
            request_completed.set()

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(invoke)
        await inner_started.wait()
        with pytest.raises(AdminMutationSettlingError):
            _ = await gated.dashboard()
        with pytest.raises(AdminMutationSettlingError):
            _ = await gated.operator_readiness()
        await request_completed.wait()

    settled = await gated.dashboard()
    operator_settled = await gated.operator_readiness()

    assert settled.runtime_state.value == "operational"
    assert operator_settled.runtime_state.value == "operational"
    assert barrier.sample() == MutationBarrierSample(active_count=0, generation=2)
    assert _status(published) == 504
    assert _error_code(published) == "admin_mutation_timeout"


async def test_completed_mutation_during_both_reads_is_rejected_directly_and_over_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_services, _ = contract_services()
    inner = base_services.credentials.repositories
    assert isinstance(inner, FakeCredentialRepositories)
    barrier = AdminMutationBarrier()
    lifecycle = RuntimeReadinessGate()
    lifecycle.set_ready(True)
    gated = GatedCredentialRepositories(inner, lifecycle, barrier)
    original_dashboard = FakeCredentialRepositories.dashboard
    original_operator = FakeCredentialRepositories.operator_readiness

    def complete_overlapping_mutation() -> None:
        barrier.enter()
        barrier.exit()

    async def overlapping_dashboard(
        fake: FakeCredentialRepositories,
    ) -> AdminDashboardRead:
        complete_overlapping_mutation()
        return await original_dashboard(fake)

    async def overlapping_operator(
        fake: FakeCredentialRepositories,
    ) -> AdminOperatorReadinessRead:
        complete_overlapping_mutation()
        return await original_operator(fake)

    monkeypatch.setattr(FakeCredentialRepositories, "dashboard", overlapping_dashboard)
    monkeypatch.setattr(
        FakeCredentialRepositories,
        "operator_readiness",
        overlapping_operator,
    )

    with pytest.raises(AdminMutationSettlingError):
        _ = await gated.dashboard()
    with pytest.raises(AdminMutationSettlingError):
        _ = await gated.operator_readiness()

    credentials = replace(
        base_services.credentials,
        repositories=gated,
        lifecycle=lifecycle,
        mutation_barrier=barrier,
    )
    services = replace(base_services, credentials=credentials)
    headers = {
        "Host": ACCEPTED_HOST,
        "Origin": ACCEPTED_ORIGIN,
        **bearer(ADMIN_TOKEN),
    }
    with TestClient(create_app(services), base_url="http://127.0.0.1:2456") as client:
        dashboard = client.get("/admin/api/v1/dashboard", headers=headers)
        operator = client.get("/admin/api/v1/operator-readiness", headers=headers)

    assert dashboard.status_code == 503
    assert ErrorEnvelope.model_validate_json(dashboard.content).error.code == (
        "admin_mutation_settling"
    )
    assert operator.status_code == 503
    assert ErrorEnvelope.model_validate_json(operator.content).error.code == (
        "admin_mutation_settling"
    )
    assert barrier.sample().active_count == 0


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("overflow", id="over-64-kib"),
        pytest.param("partial", id="partial-response"),
        pytest.param("body-before-start", id="malformed-order"),
    ],
)
async def test_invalid_or_partial_response_is_never_published(
    failure: str,
) -> None:
    barrier = AdminMutationBarrier()
    published: list[Message] = []

    async def inner(scope: Scope, inner_receive: Receive, send: Send) -> None:
        del scope, inner_receive
        if failure == "body-before-start":
            await send({"type": "http.response.body", "body": b"invalid", "more_body": False})
            return
        await send({"type": "http.response.start", "status": 201, "headers": []})
        if failure == "overflow":
            await send(
                {
                    "type": "http.response.body",
                    "body": b"x" * 65_537,
                    "more_body": False,
                }
            )
            return
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})

    async def send(message: Message) -> None:
        published.append(message)

    middleware = AdminMutationBoundaryMiddleware(
        inner,
        barrier=barrier,
        lifecycle=_Lifecycle(ready=True),
        deadline_seconds=5,
    )
    await middleware(_scope(), _receive(), send)

    assert len(published) == 2
    assert _status(published) == 502
    assert _error_code(published) == "admin_mutation_response_invalid"
    assert b"partial" not in _body(published)


async def test_authentication_401_is_outside_boundary_but_authenticated_422_is_inside() -> None:
    services, _ = contract_services()
    barrier = services.credentials.mutation_barrier
    accepted = {"Host": ACCEPTED_HOST, "Origin": ACCEPTED_ORIGIN}
    with TestClient(create_app(services), base_url="http://127.0.0.1:2456") as client:
        before = barrier.sample()
        unauthorized = client.post(
            _MUTATION_PATH,
            headers=accepted,
            json={"key": "synthetic"},
        )
        after_401 = barrier.sample()
        invalid = client.post(
            _MUTATION_PATH,
            headers={**accepted, **bearer(ADMIN_TOKEN)},
            json={"key": ""},
        )
        after_422 = barrier.sample()

    assert unauthorized.status_code == 401
    assert after_401 == before
    assert invalid.status_code == 422
    assert after_422.active_count == 0
    assert after_422.generation == before.generation + 2
