"""Strict response-header validation at the NVIDIA adapter boundary."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import h11
import pytest

from nvidia_build_lb.headers import (
    HeaderProtocolError,
    MediaType,
    validate_upstream_headers,
)

pytestmark = pytest.mark.nvidia_routing

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_REQUEST_ID = "request_202-safe"


def _headers(*pairs: tuple[bytes, bytes]) -> tuple[tuple[bytes, bytes], ...]:
    return pairs


def test_terminal_json_headers_are_normalized_without_exposing_transfer_framing() -> None:
    validated = validate_upstream_headers(
        status_code=200,
        raw_headers=_headers(
            (b"Content-Type", b' application/json; charset="utf-8" '),
            (b"Content-Encoding", b"IDENTITY"),
            (b"Transfer-Encoding", b"chunked"),
        ),
        now=_NOW,
    )

    assert validated.media_type is MediaType.JSON
    assert validated.content_length is None
    assert validated.retry_after_seconds is None
    assert validated.request_id is None
    assert validated.application_headers == ((b"Content-Type", b"application/json"),)


@pytest.mark.parametrize(
    ("name", "values"),
    [
        (b"Content-Type", (b"application/json", b"application/json")),
        (b"Content-Length", (b"1", b"1")),
        (b"Content-Encoding", (b"identity", b"identity")),
        (b"Transfer-Encoding", (b"chunked", b"chunked")),
    ],
)
def test_security_relevant_header_multiplicity_is_rejected(
    name: bytes,
    values: tuple[bytes, ...],
) -> None:
    raw = [(b"Content-Type", b"application/json")]
    if name == b"Content-Type":
        raw.clear()
    raw.extend((name, value) for value in values)

    with pytest.raises(HeaderProtocolError):
        _ = validate_upstream_headers(status_code=200, raw_headers=tuple(raw), now=_NOW)


@pytest.mark.parametrize(
    "value",
    [b"", b"00", b"+1", b"9223372036854775808", b"1, 1"],
)
def test_content_length_has_exact_ascii_int63_grammar(value: bytes) -> None:
    with pytest.raises(HeaderProtocolError):
        _ = validate_upstream_headers(
            status_code=200,
            raw_headers=_headers(
                (b"Content-Type", b"application/json"),
                (b"Content-Length", value),
            ),
            now=_NOW,
        )


def test_transport_normalized_outer_whitespace_is_not_part_of_content_length_grammar() -> None:
    validated = validate_upstream_headers(
        status_code=200,
        raw_headers=_headers(
            (b"Content-Type", b"application/json"),
            (b"Content-Length", b" \t1\t "),
        ),
        now=_NOW,
    )

    assert validated.content_length == 1


def test_content_length_and_transfer_encoding_are_mutually_exclusive() -> None:
    with pytest.raises(HeaderProtocolError):
        _ = validate_upstream_headers(
            status_code=200,
            raw_headers=_headers(
                (b"Content-Type", b"application/json"),
                (b"Content-Length", b"4"),
                (b"Transfer-Encoding", b"chunked"),
            ),
            now=_NOW,
        )


@pytest.mark.parametrize(
    "value",
    [b"application/json, text/plain", b"text/plain", b"application/json; A=1; a=2"],
)
def test_terminal_content_type_rejects_ambiguous_or_unsupported_values(value: bytes) -> None:
    with pytest.raises(HeaderProtocolError):
        _ = validate_upstream_headers(
            status_code=200,
            raw_headers=_headers((b"Content-Type", value)),
            now=_NOW,
        )


@pytest.mark.parametrize(
    ("status_code", "content_type"),
    [
        (429, b"text/plain"),
        (503, b"application/problem+json"),
    ],
)
def test_error_status_ignores_unconsumed_body_media_type(
    status_code: int,
    content_type: bytes,
) -> None:
    validated = validate_upstream_headers(
        status_code=status_code,
        raw_headers=_headers(
            (b"Content-Type", content_type),
            (b"Content-Length", b"7"),
        ),
        now=_NOW,
    )

    assert validated.media_type is None
    assert validated.content_length == 7
    assert validated.application_headers == ((b"Content-Length", b"7"),)


@pytest.mark.parametrize(
    "raw_headers",
    [
        _headers((b"Content-Type", b"\xff")),
        _headers((b"Content-Type", b"text/plain")),
        _headers((b"\xff", b"value")),
    ],
)
def test_header_protocol_error_retains_no_hostile_exception_context(
    raw_headers: tuple[tuple[bytes, bytes], ...],
) -> None:
    with pytest.raises(HeaderProtocolError) as captured:
        _ = validate_upstream_headers(status_code=200, raw_headers=raw_headers, now=_NOW)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_origin_and_poll_202_request_id_contract() -> None:
    origin = validate_upstream_headers(
        status_code=202,
        raw_headers=_headers((b"NVCF-REQID", _REQUEST_ID.encode())),
        now=_NOW,
    )
    poll_without_repeat = validate_upstream_headers(
        status_code=202,
        raw_headers=(),
        now=_NOW,
        origin_request_id=_REQUEST_ID,
    )
    poll_with_repeat = validate_upstream_headers(
        status_code=202,
        raw_headers=_headers((b"nvcf-reqid", _REQUEST_ID.encode())),
        now=_NOW,
        origin_request_id=_REQUEST_ID,
    )

    assert origin.request_id == _REQUEST_ID
    assert poll_without_repeat.request_id == _REQUEST_ID
    assert poll_with_repeat.request_id == _REQUEST_ID


@pytest.mark.parametrize(
    ("value", "origin"),
    [
        (b"different", _REQUEST_ID),
        (b"bad,value", None),
        (b"-bad", None),
        (b"a" * 129, None),
        (b"\xff", None),
    ],
)
def test_202_request_id_rejects_invalid_or_changed_values(value: bytes, origin: str | None) -> None:
    with pytest.raises(HeaderProtocolError):
        _ = validate_upstream_headers(
            status_code=202,
            raw_headers=_headers((b"NVCF-REQID", value)),
            now=_NOW,
            origin_request_id=origin,
        )


@pytest.mark.parametrize(
    "value",
    [b" request-202", b"request-202 ", b"\trequest-202", b"request-202\t"],
)
def test_h11_normalizes_202_request_id_outer_ows_before_semantic_validation(
    value: bytes,
) -> None:
    connection = h11.Connection(h11.CLIENT)
    connection.receive_data(
        b"HTTP/1.1 202 Accepted\r\nNVCF-REQID:" + value + b"\r\nContent-Length: 0\r\n\r\n"
    )
    event = connection.next_event()

    assert isinstance(event, h11.Response)
    normalized_headers = tuple(event.headers.raw_items())
    assert normalized_headers[0] == (b"NVCF-REQID", b"request-202")

    validated = validate_upstream_headers(
        status_code=202,
        raw_headers=normalized_headers,
        now=_NOW,
    )

    assert validated.request_id == "request-202"


def test_retry_after_accepts_unpadded_seconds_and_exact_imf_date_with_clamp() -> None:
    seconds = validate_upstream_headers(
        status_code=429,
        raw_headers=_headers((b"Retry-After", b"600")),
        now=_NOW,
    )
    date_value = format_datetime(_NOW + timedelta(seconds=31), usegmt=True).encode("ascii")
    date = validate_upstream_headers(
        status_code=429,
        raw_headers=_headers((b"Retry-After", date_value)),
        now=_NOW,
    )
    huge_seconds = validate_upstream_headers(
        status_code=429,
        raw_headers=_headers((b"Retry-After", b"1" * 5000)),
        now=_NOW,
    )

    assert seconds.retry_after_seconds == 300
    assert date.retry_after_seconds == 31
    assert huge_seconds.retry_after_seconds == 300


@pytest.mark.parametrize("values", [(), (b"01",), (b"bogus",), (b"1", b"2")])
def test_retry_after_absent_invalid_or_multiple_uses_local_jitter(
    values: tuple[bytes, ...],
) -> None:
    validated = validate_upstream_headers(
        status_code=429,
        raw_headers=tuple((b"Retry-After", value) for value in values),
        now=_NOW,
    )

    assert validated.retry_after_seconds is None
