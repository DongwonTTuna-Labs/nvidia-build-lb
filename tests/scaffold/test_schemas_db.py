import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from nvidia_build_lb.config import NVIDIA_MODEL
from nvidia_build_lb.db import create_engine, create_session_factory
from nvidia_build_lb.schemas import (
    ChatCompletionsRequest,
    ErrorEnvelope,
    ModelListResponse,
)


def test_chat_request_preserves_an_unknown_nvidia_extension() -> None:
    # Given: a valid OpenAI request with a provider extension.
    raw_request = {
        "model": NVIDIA_MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": True},
    }

    # When: the request crosses the Pydantic boundary.
    request = ChatCompletionsRequest.model_validate(raw_request)

    # Then: known fields are typed and the extension remains intact.
    assert request.model == NVIDIA_MODEL
    assert request.messages[0].role == "user"
    serialized = request.model_dump_json()
    assert '"chat_template_kwargs":{"enable_thinking":true}' in serialized


def test_chat_request_rejects_an_empty_message_list() -> None:
    # Given: an OpenAI-shaped request without a conversation turn.
    raw_request: dict[str, JsonValue] = {"model": NVIDIA_MODEL, "messages": []}

    # When: the request crosses the typed boundary.
    with pytest.raises(ValidationError) as captured:
        _ = ChatCompletionsRequest.model_validate(raw_request)

    # Then: the invalid messages field is rejected at that boundary.
    assert captured.value.error_count() == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_tokens", 0),
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("top_p", -0.1),
        ("top_p", 1.1),
        ("frequency_penalty", -2.1),
        ("frequency_penalty", 2.1),
        ("presence_penalty", -2.1),
        ("presence_penalty", 2.1),
    ],
)
def test_chat_request_rejects_an_out_of_range_numeric_field(
    field: str,
    value: float,
) -> None:
    # Given: an otherwise valid request with one out-of-range numeric field.
    raw_request: dict[str, JsonValue] = {
        "model": NVIDIA_MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        field: value,
    }

    # When: the request crosses the typed boundary.
    with pytest.raises(ValidationError) as captured:
        _ = ChatCompletionsRequest.model_validate(raw_request)

    # Then: exactly that boundary error is reported.
    assert captured.value.error_count() == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stream", "false"),
        ("max_tokens", "1"),
        ("max_tokens", True),
        ("temperature", "0.5"),
        ("top_p", "0.5"),
        ("frequency_penalty", "0"),
        ("presence_penalty", "0"),
        ("seed", "7"),
        ("seed", False),
    ],
)
def test_chat_request_rejects_a_coercible_wrong_type_scalar(
    field: str,
    value: JsonValue,
) -> None:
    # Given: an otherwise valid request with one scalar of the wrong JSON type.
    raw_request: dict[str, JsonValue] = {
        "model": NVIDIA_MODEL,
        "messages": [{"role": "user", "content": "hello"}],
        field: value,
    }

    # When: the request crosses the typed boundary.
    with pytest.raises(ValidationError) as captured:
        _ = ChatCompletionsRequest.model_validate(raw_request)

    # Then: coercion is rejected instead of widening the documented contract.
    assert captured.value.error_count() == 1


def test_models_response_exposes_only_the_fixed_model() -> None:
    # Given: the repository's fixed NVIDIA model identity.
    expected_model = NVIDIA_MODEL

    # When: the typed model-list seam builds its response.
    response = ModelListResponse.fixed_model()

    # Then: exactly one OpenAI-shaped model card is returned.
    assert response.object == "list"
    assert len(response.data) == 1
    assert response.data[0].id == expected_model
    assert response.data[0].object == "model"


def test_error_envelope_has_the_locked_nested_shape() -> None:
    # Given: stable safe error components.
    request_id = "request-envelope"

    # When: the response seam constructs an error envelope.
    envelope = ErrorEnvelope.from_safe_parts(
        code="configuration_invalid",
        message="configuration is invalid",
        request_id=request_id,
    )

    # Then: the safe public fields are nested under error.
    assert envelope.error.code == "configuration_invalid"
    assert envelope.error.message == "configuration is invalid"
    assert envelope.error.request_id == request_id


@pytest.mark.anyio
async def test_database_factory_is_lazy_and_sessions_do_not_expire_on_commit() -> None:
    # Given: a synthetic PostgreSQL URL with no real credentials.
    database_url = SecretStr("postgresql+asyncpg://nvidia_build_lb@127.0.0.1/nvidia_build_lb")

    # When: engine and session seams are created without opening a connection.
    engine = create_engine(database_url)
    session_factory = create_session_factory(engine)

    # Then: the async driver is selected and session state stays usable after commit.
    try:
        assert engine.url.drivername == "postgresql+asyncpg"
        async with session_factory() as session:
            assert session.sync_session.expire_on_commit is False
    finally:
        await engine.dispose()
