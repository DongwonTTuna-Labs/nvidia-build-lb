"""Strict media-type parsing for terminal NVIDIA representations."""

import re
from typing import Final

from nvidia_build_lb.header_types import HeaderProtocolError, MediaType

_MIN_QUOTED_VALUE_LENGTH: Final = 2
_SPACE_CODEPOINT: Final = 0x20
_TOKEN_PATTERN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")


def parse_media_type(value: bytes | None) -> MediaType | None:
    """Parse one unambiguous supported media type."""
    if value is None:
        return None
    text = _decode_ascii(value)
    return _parse_decoded_media_type(text)


def _decode_ascii(value: bytes) -> str:
    """Decode without retaining a provider-controlled Unicode exception."""
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError:
        text = None
    if text is None:
        raise HeaderProtocolError
    return text


def _parse_decoded_media_type(text: str) -> MediaType:
    segments = _split_parameters(text)
    essence = segments[0].strip().lower()
    if "/" not in essence:
        raise HeaderProtocolError
    major, minor = essence.split("/", 1)
    if _TOKEN_PATTERN.fullmatch(major) is None or _TOKEN_PATTERN.fullmatch(minor) is None:
        raise HeaderProtocolError
    seen_parameters: set[str] = set()
    for segment in segments[1:]:
        if "=" not in segment:
            raise HeaderProtocolError
        name, parameter_value = (part.strip() for part in segment.split("=", 1))
        normalized_name = name.lower()
        if (
            _TOKEN_PATTERN.fullmatch(name) is None
            or normalized_name in seen_parameters
            or not _valid_parameter_value(parameter_value)
        ):
            raise HeaderProtocolError
        seen_parameters.add(normalized_name)
    try:
        parsed = MediaType(essence)
    except ValueError:
        parsed = None
    if parsed is None:
        raise HeaderProtocolError
    return parsed


def _split_parameters(value: str) -> tuple[str, ...]:
    segments: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for character in value:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if quoted and character == "\\":
            current.append(character)
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            current.append(character)
            continue
        if not quoted and character == ",":
            raise HeaderProtocolError
        if not quoted and character == ";":
            segments.append("".join(current))
            current.clear()
            continue
        current.append(character)
    if quoted or escaped:
        raise HeaderProtocolError
    segments.append("".join(current))
    return tuple(segments)


def _valid_parameter_value(value: str) -> bool:
    if _TOKEN_PATTERN.fullmatch(value) is not None:
        return True
    if len(value) < _MIN_QUOTED_VALUE_LENGTH or value[0] != '"' or value[-1] != '"':
        return False
    escaped = False
    for character in value[1:-1]:
        codepoint = ord(character)
        if escaped:
            if character == "\x7f" or (codepoint < _SPACE_CODEPOINT and character != "\t"):
                return False
            escaped = False
        elif character == "\\":
            escaped = True
        elif character in {'"', "\x7f"} or (codepoint < _SPACE_CODEPOINT and character != "\t"):
            return False
    return not escaped
