"""Strict NVIDIA response-header parsing and application sanitization."""

import math
import re
from datetime import UTC, datetime
from email.utils import format_datetime, parsedate_to_datetime
from typing import Final

from nvidia_build_lb.header_types import (
    HeaderProtocolError,
    MediaType,
    ValidatedUpstreamHeaders,
)
from nvidia_build_lb.media_type_parser import parse_media_type

__all__ = [
    "HeaderProtocolError",
    "MediaType",
    "ValidatedUpstreamHeaders",
    "is_valid_nvcf_request_id",
    "validate_upstream_headers",
]

_MAX_SIGNED_INT64: Final = (1 << 63) - 1
_TERMINAL_SUCCESS_STATUS: Final = 200
_ASYNC_STATUS: Final = 202
_MAX_REQUEST_ID_BYTES: Final = 128
_MAX_RETRY_AFTER_SECONDS: Final = 300
_MIN_RETRY_AFTER_SECONDS: Final = 1
_MAX_CLAMPED_RETRY_DIGITS: Final = 3
_REQUEST_ID_PATTERN: Final = re.compile(rb"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_CONTENT_LENGTH_PATTERN: Final = re.compile(rb"(?:0|[1-9][0-9]{0,18})\Z")
_RETRY_SECONDS_PATTERN: Final = re.compile(rb"(?:0|[1-9][0-9]*)\Z")


def validate_upstream_headers(
    *,
    status_code: int,
    raw_headers: tuple[tuple[bytes, bytes], ...],
    now: datetime,
    origin_request_id: str | None = None,
) -> ValidatedUpstreamHeaders:
    """Validate multiplicity, framing, representation, retry, and 202 identity."""
    fields = _collect_fields(raw_headers)
    transfer_encoding = _optional_single(fields, b"transfer-encoding")
    content_length_value = _optional_single(fields, b"content-length")
    content_encoding = _optional_single(fields, b"content-encoding")
    content_type_value = _optional_single(fields, b"content-type")

    if transfer_encoding is not None and transfer_encoding.lower() != b"chunked":
        raise HeaderProtocolError
    if transfer_encoding is not None and content_length_value is not None:
        raise HeaderProtocolError
    if content_encoding is not None and content_encoding.lower() != b"identity":
        raise HeaderProtocolError

    content_length = _parse_content_length(content_length_value)
    media_type = (
        parse_media_type(content_type_value) if status_code == _TERMINAL_SUCCESS_STATUS else None
    )
    if status_code == _TERMINAL_SUCCESS_STATUS and media_type is None:
        raise HeaderProtocolError

    retry_after = _parse_retry_after(fields.get(b"retry-after", ()), now)
    request_id = _validate_202_request_id(
        status_code=status_code,
        values=fields.get(b"nvcf-reqid", ()),
        origin_request_id=origin_request_id,
    )
    application_headers = _application_headers(media_type, content_length, retry_after)
    return ValidatedUpstreamHeaders(
        media_type=media_type,
        content_length=content_length,
        retry_after_seconds=retry_after,
        request_id=request_id,
        application_headers=application_headers,
    )


def is_valid_nvcf_request_id(value: str) -> bool:
    """Check the closed ASCII request-ID grammar shared by origin and poll."""
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return (
        len(encoded) <= _MAX_REQUEST_ID_BYTES and _REQUEST_ID_PATTERN.fullmatch(encoded) is not None
    )


def _collect_fields(
    raw_headers: tuple[tuple[bytes, bytes], ...],
) -> dict[bytes, tuple[bytes, ...]]:
    mutable: dict[bytes, list[bytes]] = {}
    for raw_name, raw_value in raw_headers:
        try:
            name = raw_name.lower().decode("ascii").encode("ascii")
        except UnicodeError:
            name = None
        if name is None:
            raise HeaderProtocolError
        value = raw_value.strip(b" \t")
        mutable.setdefault(name, []).append(value)
    return {name: tuple(values) for name, values in mutable.items()}


def _optional_single(fields: dict[bytes, tuple[bytes, ...]], name: bytes) -> bytes | None:
    values = fields.get(name, ())
    if len(values) > 1:
        raise HeaderProtocolError
    return None if not values else values[0]


def _parse_content_length(value: bytes | None) -> int | None:
    if value is None:
        return None
    if _CONTENT_LENGTH_PATTERN.fullmatch(value) is None:
        raise HeaderProtocolError
    parsed = int(value)
    if parsed > _MAX_SIGNED_INT64:
        raise HeaderProtocolError
    return parsed


def _parse_retry_after(values: tuple[bytes, ...], now: datetime) -> int | None:
    if len(values) != 1:
        return None
    value = values[0]
    if _RETRY_SECONDS_PATTERN.fullmatch(value) is not None:
        seconds = _MAX_RETRY_AFTER_SECONDS if len(value) > _MAX_CLAMPED_RETRY_DIGITS else int(value)
        return _clamp_retry(seconds)
    try:
        text = value.decode("ascii")
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if parsed.tzinfo is None:
        return None
    normalized = parsed.astimezone(UTC)
    if format_datetime(normalized, usegmt=True) != text:
        return None
    seconds = math.ceil((normalized - now.astimezone(UTC)).total_seconds())
    return _clamp_retry(seconds)


def _clamp_retry(seconds: int) -> int:
    return min(_MAX_RETRY_AFTER_SECONDS, max(_MIN_RETRY_AFTER_SECONDS, seconds))


def _validate_202_request_id(
    *,
    status_code: int,
    values: tuple[bytes, ...],
    origin_request_id: str | None,
) -> str | None:
    if status_code != _ASYNC_STATUS:
        return origin_request_id
    if origin_request_id is None:
        if len(values) != 1 or _REQUEST_ID_PATTERN.fullmatch(values[0]) is None:
            raise HeaderProtocolError
        return values[0].decode("ascii")
    if not is_valid_nvcf_request_id(origin_request_id) or len(values) > 1:
        raise HeaderProtocolError
    if values:
        try:
            repeated = values[0].decode("ascii")
        except UnicodeDecodeError:
            repeated = None
        if repeated is None:
            raise HeaderProtocolError
        if repeated != origin_request_id or b"," in values[0]:
            raise HeaderProtocolError
    return origin_request_id


def _application_headers(
    media_type: MediaType | None,
    content_length: int | None,
    retry_after_seconds: int | None,
) -> tuple[tuple[bytes, bytes], ...]:
    headers: list[tuple[bytes, bytes]] = []
    if media_type is not None:
        headers.append((b"Content-Type", media_type.value.encode("ascii")))
    if content_length is not None:
        headers.append((b"Content-Length", str(content_length).encode("ascii")))
    if retry_after_seconds is not None:
        headers.append((b"Retry-After", str(retry_after_seconds).encode("ascii")))
    return tuple(headers)
