"""OpenAI-compatible chat success, streaming, and safe-error contracts."""

import pytest
from pydantic import JsonValue

from ._support import CHAT_TOKEN, ContractClient, assert_error, assert_excludes, bearer


def _chat_body(*, stream: bool, content: str = "contract success") -> dict[str, JsonValue]:
    return {
        "model": "z-ai/glm-5.2",
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
        "chat_template_kwargs": {"enable_thinking": True},
    }


def test_chat_nonstream_preserves_safe_json(contract_client: ContractClient) -> None:
    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body=_chat_body(stream=False),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.content == (
        b'{"id":"contract-chat","object":"chat.completion","model":"z-ai/glm-5.2",'
        b'"choices":[{"index":0,"message":{"role":"assistant","content":"ok"}}]}'
    )


def test_chat_stream_preserves_sse_and_done(contract_client: ContractClient) -> None:
    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body=_chat_body(stream=True),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == (
        b'data: {"id":"contract-chat","choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
    )


def test_chat_wrong_model_is_safe_404(contract_client: ContractClient) -> None:
    body = _chat_body(stream=False)
    body["model"] = "not-the-fixed-model"
    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body=body,
    )

    assert_error(response, status_code=404, code="model_not_found")


def test_chat_invalid_boundary_is_safe_422(contract_client: ContractClient) -> None:
    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body={"model": "z-ai/glm-5.2", "messages": [], "stream": "false"},
    )

    assert_error(response, status_code=422, code="invalid_request")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("stream", "false", id="stream-string"),
        pytest.param("max_tokens", "1", id="max-tokens-string"),
        pytest.param("max_tokens", True, id="max-tokens-boolean"),
        pytest.param("temperature", "0.5", id="temperature-string"),
        pytest.param("top_p", "0.5", id="top-p-string"),
        pytest.param("frequency_penalty", "0", id="frequency-penalty-string"),
        pytest.param("presence_penalty", "0", id="presence-penalty-string"),
        pytest.param("seed", "7", id="seed-string"),
        pytest.param("seed", False, id="seed-boolean"),
    ],
)
def test_chat_rejects_each_wrong_type_scalar_at_composed_boundary(
    contract_client: ContractClient,
    field: str,
    value: JsonValue,
) -> None:
    body = _chat_body(stream=False)
    body[field] = value

    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body=body,
    )

    assert_error(response, status_code=422, code="invalid_request")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("max_tokens", 0, id="max-tokens-zero"),
        pytest.param("temperature", -0.1, id="temperature-low"),
        pytest.param("temperature", 2.1, id="temperature-high"),
        pytest.param("top_p", -0.1, id="top-p-low"),
        pytest.param("top_p", 1.1, id="top-p-high"),
        pytest.param("frequency_penalty", -2.1, id="frequency-penalty-low"),
        pytest.param("frequency_penalty", 2.1, id="frequency-penalty-high"),
        pytest.param("presence_penalty", -2.1, id="presence-penalty-low"),
        pytest.param("presence_penalty", 2.1, id="presence-penalty-high"),
    ],
)
def test_chat_rejects_each_out_of_range_scalar_at_composed_boundary(
    contract_client: ContractClient,
    field: str,
    value: float,
) -> None:
    body = _chat_body(stream=False)
    body[field] = value

    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body=body,
    )

    assert_error(response, status_code=422, code="invalid_request")


@pytest.mark.parametrize(
    ("scenario", "status_code", "code"),
    [
        pytest.param("application-json-error", 502, "upstream_internal_error", id="json"),
        pytest.param("problem-json-error", 503, "upstream_unavailable", id="problem-json"),
        pytest.param("text-plain-error", 429, "upstream_rate_limited", id="text"),
    ],
)
def test_chat_upstream_error_body_is_replaced_by_safe_json(
    contract_client: ContractClient,
    scenario: str,
    status_code: int,
    code: str,
) -> None:
    unsafe_body = f"provider-secret-body-{scenario}"
    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body=_chat_body(stream=False, content=unsafe_body),
    )

    assert_error(response, status_code=status_code, code=code)
    assert_excludes(response, unsafe_body)


def test_chat_midstream_failure_emits_one_safe_terminal_event(
    contract_client: ContractClient,
) -> None:
    response = contract_client.request(
        "POST",
        "/v1/chat/completions",
        headers=bearer(CHAT_TOKEN),
        json_body=_chat_body(stream=True, content="contract-midstream-failure"),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    expected_terminal = b"".join(
        (
            b'event: error\ndata: {"error":{"code":"upstream_stream_error",',
            b'"message":"upstream stream ended unexpectedly",',
            b'"request_id":"contract-request"}}\n\n',
        )
    )
    assert response.content.endswith(expected_terminal)
