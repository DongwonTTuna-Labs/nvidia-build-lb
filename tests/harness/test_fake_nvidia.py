"""Green unit checks for the socket-free NVIDIA ASGI harness."""

import pytest
from fastapi.testclient import TestClient

from .fake_nvidia import FakeNvidiaHarness, ObservedRequest, ScriptedResponse

pytestmark = pytest.mark.nvidia_routing


def test_fake_nvidia_is_lazy_and_records_only_safe_request_metadata() -> None:
    response_script = ScriptedResponse.json(
        {"id": "fake-result"},
        headers=(("NVCF-REQID", "request-one"),),
    )
    harness = FakeNvidiaHarness((response_script,))

    assert harness.observed == ()
    assert harness.pending_count == 1

    with TestClient(harness.app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer synthetic-not-recorded"},
            content=b'{"messages":[{"content":"not-recorded"}]}',
        )

    assert response.status_code == 200
    assert response.content == b'{"id":"fake-result"}'
    assert response.headers.get_list("nvcf-reqid") == ["request-one"]
    assert harness.observed == (
        ObservedRequest(method="POST", path="/v1/chat/completions", body_size=41),
    )
    assert harness.pending_count == 0


def test_fake_nvidia_preserves_sse_chunk_order() -> None:
    harness = FakeNvidiaHarness((ScriptedResponse.sse(b": heartbeat\n\n", b"data: [DONE]\n\n"),))

    with TestClient(harness.app) as client:
        response = client.post("/v1/chat/completions")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == b": heartbeat\n\ndata: [DONE]\n\n"


def test_fake_nvidia_queue_exhaustion_is_deterministic() -> None:
    harness = FakeNvidiaHarness()

    with TestClient(harness.app) as client:
        response = client.get("/v2/nvcf/pexec/status/request-one")

    assert response.status_code == 500
    assert response.content == b'{"error":"fake response queue exhausted"}'
