"""Safe NVIDIA response-header types and protocol error."""

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import override


@unique
class MediaType(StrEnum):
    """The only terminal NVIDIA representations accepted."""

    JSON = "application/json"
    SSE = "text/event-stream"


class HeaderProtocolError(Exception):
    """Reject ambiguous framing without retaining header values."""

    @override
    def __str__(self) -> str:
        return "upstream_header_protocol_error"


@dataclass(frozen=True, slots=True)
class ValidatedUpstreamHeaders:
    """Normalized metadata safe to expose past the response hook."""

    media_type: MediaType | None
    content_length: int | None
    retry_after_seconds: int | None
    request_id: str | None
    application_headers: tuple[tuple[bytes, bytes], ...]
