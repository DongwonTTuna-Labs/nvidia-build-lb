"""Incremental strict SSE framing independent of transport chunks."""

import pytest

from nvidia_build_lb.sse import (
    MAX_SSE_TRANSPORT_CHUNK_BYTES,
    SSEFrameParser,
    SSEProtocolError,
)

pytestmark = pytest.mark.nvidia_routing


def test_parser_is_chunk_independent_and_assembles_multiline_done_exactly() -> None:
    parser = SSEFrameParser(max_frame_bytes=128)

    first = parser.feed(b'data: {"choices":[]}')
    second = parser.feed(b"\r\n\r")
    third = parser.feed(b"\ndata: [DO\n")
    fourth = parser.feed(b"data: NE]\n\n")

    assert first == ()
    assert second == ()
    assert len(third) == 1
    assert third[0].data == b'{"choices":[]}'
    assert third[0].done is False
    assert len(fourth) == 1
    assert fourth[0].data == b"[DO\nNE]"
    assert fourth[0].done is False
    with pytest.raises(SSEProtocolError):
        parser.finish()


def test_done_frame_is_forwarded_once_and_suffix_is_never_inspected() -> None:
    parser = SSEFrameParser(max_frame_bytes=64)

    frames = parser.feed(b"data:[DONE]\n\n\xff\rbroken")

    assert len(frames) == 1
    assert frames[0].raw == b"data:[DONE]\n\n"
    assert frames[0].data == b"[DONE]"
    assert frames[0].done is True
    assert parser.done is True
    parser.finish(expected_content_length=999)
    assert parser.feed(b"ignored") == ()


def test_coalesced_transport_chunk_drains_one_frame_per_consumer_step() -> None:
    parser = SSEFrameParser(max_frame_bytes=64)

    first = parser.feed(b"data: one\n\ndata: two\n\n")
    second = parser.feed(b"")

    assert len(first) == 1
    assert first[0].data == b"one"
    assert len(second) == 1
    assert second[0].data == b"two"


def test_bom_counts_toward_bound_but_is_removed_only_for_logical_first_line() -> None:
    parser = SSEFrameParser(max_frame_bytes=32)

    frames = parser.feed(b"\xef\xbb\xbfdata: ok\r\n\r\n")

    assert frames[0].raw.startswith(b"\xef\xbb\xbf")
    assert frames[0].data == b"ok"


@pytest.mark.parametrize(
    "payload",
    [
        b"data: bad\rvalue\n\n",
        b"data: \xff\n\n",
        b"\xef\xbb\xbf\xef\xbb\xbfdata: bad\n\n",
    ],
)
def test_parser_rejects_bare_cr_invalid_utf8_and_second_leading_bom(payload: bytes) -> None:
    parser = SSEFrameParser(max_frame_bytes=64)

    with pytest.raises(SSEProtocolError):
        _ = parser.feed(payload)


def test_parser_rejects_oversize_incomplete_or_complete_frame() -> None:
    incomplete = SSEFrameParser(max_frame_bytes=8)
    complete = SSEFrameParser(max_frame_bytes=8)

    with pytest.raises(SSEProtocolError):
        _ = incomplete.feed(b"data: 123")
    with pytest.raises(SSEProtocolError):
        _ = complete.feed(b"data: 1\n\n")


def test_eof_without_done_validates_content_length_then_fails() -> None:
    parser = SSEFrameParser(max_frame_bytes=64)
    _ = parser.feed(b"data: one\n\n")

    with pytest.raises(SSEProtocolError):
        parser.finish(expected_content_length=999)

    exact = SSEFrameParser(max_frame_bytes=64)
    payload = b"data: one\n\n"
    _ = exact.feed(payload)
    with pytest.raises(SSEProtocolError):
        exact.finish(expected_content_length=len(payload))


def test_one_byte_chunks_are_scanned_once_instead_of_from_buffer_start() -> None:
    parser = SSEFrameParser(max_frame_bytes=16 * 1024)
    payload = b"data: " + (b"x" * 10_000)

    for value in payload:
        assert parser.feed(bytes((value,))) == ()

    assert parser.scan_work_bytes == len(payload)


def test_transport_chunk_cap_rejects_unbounded_coalesced_suffix_without_retention() -> None:
    parser = SSEFrameParser(max_frame_bytes=1024 * 1024)
    oversized = b"x" * (MAX_SSE_TRANSPORT_CHUNK_BYTES + 1)

    with pytest.raises(SSEProtocolError) as captured:
        _ = parser.feed(oversized)

    assert parser.feed(b"data: ok\n\n")[0].data == b"ok"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_invalid_utf8_sse_error_retains_no_provider_exception_context() -> None:
    parser = SSEFrameParser(max_frame_bytes=64)

    with pytest.raises(SSEProtocolError) as captured:
        _ = parser.feed(b"data: \xff\n\n")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
