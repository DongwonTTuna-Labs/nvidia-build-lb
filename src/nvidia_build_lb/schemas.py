"""Typed public boundary schemas shared by later API composition."""

from typing import Annotated, ClassVar, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue

from nvidia_build_lb.config import NVIDIA_MODEL

type StrictBoolean = Annotated[bool, Field(strict=True)]
type StrictInteger = Annotated[int, Field(strict=True)]
type PositiveTokenCount = Annotated[int, Field(ge=1, strict=True)]


def _accept_json_integer(value: object) -> object:
    """Accept JSON integer numbers without opening string/bool coercion."""
    if isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


type JsonNumber = Annotated[float, BeforeValidator(_accept_json_integer)]
type SamplingTemperature = Annotated[JsonNumber, Field(ge=0.0, le=2.0, strict=True)]
type UnitInterval = Annotated[JsonNumber, Field(ge=0.0, le=1.0, strict=True)]
type Penalty = Annotated[JsonNumber, Field(ge=-2.0, le=2.0, strict=True)]


class ChatMessage(BaseModel):
    """An OpenAI-compatible message with NVIDIA extension preservation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="allow")

    role: str
    content: JsonValue
    name: str | None = None
    tool_call_id: str | None = None


class ChatCompletionsRequest(BaseModel):
    """The supported chat fields while retaining unknown NVIDIA extensions."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="allow")

    model: str
    messages: Annotated[tuple[ChatMessage, ...], Field(min_length=1)]
    stream: StrictBoolean = False
    max_tokens: PositiveTokenCount | None = None
    temperature: SamplingTemperature | None = None
    top_p: UnitInterval | None = None
    frequency_penalty: Penalty | None = None
    presence_penalty: Penalty | None = None
    stop: str | tuple[str, ...] | None = None
    seed: StrictInteger | None = None
    user: str | None = None
    tools: tuple[JsonValue, ...] | None = None
    tool_choice: JsonValue = None
    response_format: JsonValue = None


class ModelCard(BaseModel):
    """One OpenAI-compatible fixed model descriptor."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    id: Literal["z-ai/glm-5.2"] = NVIDIA_MODEL
    object: Literal["model"] = "model"
    owned_by: Literal["nvidia"] = "nvidia"


class ModelListResponse(BaseModel):
    """OpenAI-compatible model list containing the fixed NVIDIA target."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    object: Literal["list"] = "list"
    data: tuple[ModelCard, ...]

    @classmethod
    def fixed_model(cls) -> Self:
        """Build the only model list this single-provider service exposes."""
        return cls(data=(ModelCard(),))


class ErrorDetail(BaseModel):
    """Safe deterministic error fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    """Shared public and admin nested error shape."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    error: ErrorDetail

    @classmethod
    def from_safe_parts(cls, code: str, message: str, request_id: str) -> Self:
        """Construct an envelope from already-whitelisted boundary data."""
        return cls(error=ErrorDetail(code=code, message=message, request_id=request_id))
