"""Bounded raw response consumption and strict JSON representations."""

import json
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final, Protocol, override, runtime_checkable

from pydantic import JsonValue, TypeAdapter, ValidationError

MAX_JSON_REPRESENTATION_BYTES: Final = 8 * 1024 * 1024
MAX_SSE_FRAME_BYTES: Final = 1024 * 1024
_UTF8_BOM: Final = b"\xef\xbb\xbf"
_REPRESENTATION_ERROR = "upstream_representation_protocol_error"
_JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


class RawResponse(Protocol):
    """The only response-body surface allowed past the sanitized hook."""

    def aiter_raw(self) -> AsyncIterator[bytes]:
        """Yield raw representation bytes after transfer framing removal."""
        ...


@runtime_checkable
class _JsonLoader(Protocol):
    def __call__(
        self,
        value: str,
        *,
        object_pairs_hook: Callable[[list[tuple[str, object]]], dict[str, object]],
        parse_constant: Callable[[str], object],
        parse_float: Callable[[str], object],
    ) -> object:
        """Parse one JSON value through exact injected hooks."""
        ...


class RepresentationProtocolError(Exception):
    """Reject unsafe or malformed provider-controlled representations."""

    @override
    def __str__(self) -> str:
        """Return one safe code without body or exception details."""
        return _REPRESENTATION_ERROR


@dataclass(frozen=True, slots=True)
class BoundedResponseReader:
    """Consume exactly one raw iterator with a hard representation bound."""

    max_bytes: int
    expected_content_length: int | None

    def __post_init__(self) -> None:
        """Reject invalid local bounds before touching a response."""
        if self.max_bytes < 1 or (
            self.expected_content_length is not None and self.expected_content_length < 0
        ):
            raise ValueError

    async def read(self, response: RawResponse) -> bytes:
        """Read through EOF, enforcing the bound before each buffer extension."""
        body = bytearray()
        async for chunk in response.aiter_raw():
            if len(body) + len(chunk) > self.max_bytes:
                raise RepresentationProtocolError
            body.extend(chunk)
        if self.expected_content_length is not None and len(body) != self.expected_content_length:
            raise RepresentationProtocolError
        return bytes(body)


@dataclass(frozen=True, slots=True)
class JsonRepresentation:
    """Original bytes paired with a validated exact-dict JSON value."""

    raw: bytes = field(repr=False)
    value: dict[str, JsonValue] = field(repr=False)


def read_json_representation(raw: bytes) -> JsonRepresentation:
    """Parse one strict RFC 8259 object while rejecting duplicate names."""
    if len(raw) > MAX_JSON_REPRESENTATION_BYTES or raw.startswith(_UTF8_BOM):
        raise RepresentationProtocolError
    loader = _json_loader()
    validated: JsonValue = None
    try:
        text = raw.decode("utf-8", errors="strict")
        parsed = loader(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite,
            parse_float=_parse_finite_float,
        )
        validated = _JSON_VALUE_ADAPTER.validate_python(parsed)
        _validate_unicode_scalars(validated)
    except (
        RecursionError,
        UnicodeDecodeError,
        ValueError,
        ValidationError,
        RepresentationProtocolError,
    ):
        invalid = True
    else:
        invalid = False
    if invalid:
        raise RepresentationProtocolError
    if not isinstance(validated, dict):
        raise RepresentationProtocolError
    return JsonRepresentation(raw=raw, value=validated)


def json_to_sse_frames(
    representation: JsonRepresentation,
) -> tuple[bytes, bytes]:
    """Convert one strict non-stream JSON object to one chunk and one DONE frame."""
    root = representation.value
    identifier = root.get("id")
    choices = root.get("choices")
    usage = root.get("usage")
    if (
        not isinstance(identifier, str)
        or not isinstance(choices, list)
        or not isinstance(usage, dict)
    ):
        raise RepresentationProtocolError

    transformed_choices: list[JsonValue] = []
    for choice in choices:
        if not isinstance(choice, dict) or "delta" in choice:
            raise RepresentationProtocolError
        message = choice.get("message")
        if not isinstance(message, dict):
            raise RepresentationProtocolError
        transformed = {name: value for name, value in choice.items() if name != "message"}
        transformed["delta"] = message
        transformed_choices.append(transformed)

    transformed_root = dict(root)
    transformed_root["object"] = "chat.completion.chunk"
    transformed_root["choices"] = transformed_choices
    payload = b""
    try:
        payload = json.dumps(
            transformed_root,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError):
        invalid = True
    else:
        invalid = False
    if invalid:
        raise RepresentationProtocolError
    first = b"data: " + payload + b"\n\n"
    if len(first) > MAX_SSE_FRAME_BYTES:
        raise RepresentationProtocolError
    return first, b"data: [DONE]\n\n"


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise RepresentationProtocolError
        result[name] = value
    return result


def _reject_nonfinite(_value: str) -> object:
    raise RepresentationProtocolError


def _parse_finite_float(value: str) -> object:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RepresentationProtocolError
    return parsed


def _validate_unicode_scalars(value: JsonValue) -> None:
    if isinstance(value, str):
        try:
            _ = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            valid = False
        else:
            valid = True
        if not valid:
            raise RepresentationProtocolError
        return
    if isinstance(value, list):
        for item in value:
            _validate_unicode_scalars(item)
        return
    if isinstance(value, dict):
        for name, item in value.items():
            _validate_unicode_scalars(name)
            _validate_unicode_scalars(item)


def _erase_type(value: object) -> object:
    return value


def _json_loader() -> _JsonLoader:
    loader = _erase_type(json.loads)
    if not isinstance(loader, _JsonLoader):
        raise RepresentationProtocolError
    return loader
