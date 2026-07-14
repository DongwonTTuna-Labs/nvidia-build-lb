"""Incremental strict Server-Sent Events framing for NVIDIA streams."""

from dataclasses import dataclass, field
from typing import ClassVar, Final, override

_UTF8_BOM: Final = b"\xef\xbb\xbf"
_SSE_ERROR = "upstream_sse_protocol_error"
_CR: Final = 13
_LF: Final = 10
MAX_SSE_TRANSPORT_CHUNK_BYTES: Final = 64 * 1024


class SSEProtocolError(Exception):
    """Reject malformed SSE without retaining frame content."""

    @override
    def __str__(self) -> str:
        """Return one safe fixed code."""
        return _SSE_ERROR


@dataclass(frozen=True, slots=True)
class SSEFrame:
    """One exact complete frame plus its assembled data discriminator."""

    raw: bytes = field(repr=False)
    data: bytes | None = field(repr=False)
    done: bool


class SSEFrameParser:
    """Parse one logical SSE stream with a one-frame pending bound."""

    __slots__: ClassVar[tuple[str, ...]] = (
        "_buffer",
        "_consumed_bytes",
        "_first_frame",
        "_frame_start",
        "_line_start",
        "_max_frame_bytes",
        "_scan_position",
        "_scan_work_bytes",
        "done",
    )

    _buffer: bytearray
    _consumed_bytes: int
    _frame_start: int
    _first_frame: bool
    _line_start: int
    _max_frame_bytes: int
    _scan_position: int
    _scan_work_bytes: int
    done: bool

    def __init__(self, *, max_frame_bytes: int) -> None:
        """Initialize one empty bounded logical stream parser."""
        if max_frame_bytes < 1:
            raise ValueError
        self._buffer = bytearray()
        self._consumed_bytes = 0
        self._frame_start = 0
        self._first_frame = True
        self._line_start = 0
        self._max_frame_bytes = max_frame_bytes
        self._scan_position = 0
        self._scan_work_bytes = 0
        self.done = False

    def feed(self, chunk: bytes) -> tuple[SSEFrame, ...]:
        """Append one transport chunk and drain at most one complete frame."""
        if self.done:
            return ()
        if len(chunk) > MAX_SSE_TRANSPORT_CHUNK_BYTES:
            raise SSEProtocolError
        if chunk:
            self._buffer.extend(chunk)
        frame_end = self._scan_frame_end()
        if frame_end is None:
            if len(self._buffer) - self._frame_start > self._max_frame_bytes:
                raise SSEProtocolError
            return ()
        raw = bytes(self._buffer[self._frame_start : frame_end])
        frame = self._parse(raw)
        self._consumed_bytes += len(raw)
        self._frame_start = frame_end
        self._line_start = frame_end
        self._scan_position = frame_end
        if frame.done:
            self.done = True
            self._buffer.clear()
        elif self._frame_start == len(self._buffer):
            self._reset_empty_buffer()
        elif self._frame_start >= MAX_SSE_TRANSPORT_CHUNK_BYTES:
            self._compact_buffer()
        return (frame,)

    def finish(self, *, expected_content_length: int | None = None) -> None:
        """Accept DONE, otherwise validate full-EOF length and fail closed."""
        if self.done:
            return
        observed_length = self._consumed_bytes + len(self._buffer) - self._frame_start
        if expected_content_length is not None and observed_length != expected_content_length:
            raise SSEProtocolError
        raise SSEProtocolError

    @property
    def scan_work_bytes(self) -> int:
        """Expose monotonic scanner work for the linear-time parser invariant."""
        return self._scan_work_bytes

    def _scan_frame_end(self) -> int | None:
        """Advance the monotonic cursor until one blank-line boundary or input end."""
        while self._scan_position < len(self._buffer):
            self._scan_work_bytes += 1
            value = self._buffer[self._scan_position]
            if value == _CR:
                if self._scan_position + 1 == len(self._buffer):
                    return None
                if self._buffer[self._scan_position + 1] != _LF:
                    raise SSEProtocolError
                self._scan_position += 1
                continue
            if value == _LF:
                content_end = self._scan_position
                if content_end > self._line_start and self._buffer[content_end - 1] == _CR:
                    content_end -= 1
                self._scan_position += 1
                if self._scan_position - self._frame_start > self._max_frame_bytes:
                    raise SSEProtocolError
                if content_end == self._line_start:
                    return self._scan_position
                self._line_start = self._scan_position
                continue
            self._scan_position += 1
            if self._scan_position - self._frame_start > self._max_frame_bytes:
                raise SSEProtocolError
        return None

    def _reset_empty_buffer(self) -> None:
        self._buffer.clear()
        self._frame_start = 0
        self._line_start = 0
        self._scan_position = 0

    def _compact_buffer(self) -> None:
        consumed = self._frame_start
        del self._buffer[:consumed]
        self._frame_start = 0
        self._line_start -= consumed
        self._scan_position -= consumed

    def _parse(self, raw: bytes) -> SSEFrame:
        logical = raw
        if self._first_frame:
            self._first_frame = False
            if logical.startswith(_UTF8_BOM):
                logical = logical[len(_UTF8_BOM) :]
                if logical.startswith(_UTF8_BOM):
                    raise SSEProtocolError
        try:
            text = logical.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            text = None
        if text is None:
            raise SSEProtocolError
        normalized = text.replace("\r\n", "\n")
        data_values: list[str] = []
        for line in normalized.split("\n"):
            if not line or line.startswith(":"):
                continue
            field_name, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field_name == "data":
                data_values.append(value)
        data_text = "\n".join(data_values) if data_values else None
        data = None if data_text is None else data_text.encode("utf-8")
        return SSEFrame(raw=raw, data=data, done=data == b"[DONE]")
