"""Strict RFC 8259 representation and JSON-to-SSE conversion contracts."""

import pytest

from nvidia_build_lb.representations import (
    RepresentationProtocolError,
    json_to_sse_frames,
    read_json_representation,
)

pytestmark = pytest.mark.nvidia_routing


def test_json_parser_preserves_raw_bytes_and_rejects_duplicate_names_at_any_depth() -> None:
    raw = b' \r\n{"id":"one","choices":[],"usage":{}}\t'
    parsed = read_json_representation(raw)

    assert parsed.raw == raw
    assert parsed.value["id"] == "one"

    with pytest.raises(RepresentationProtocolError):
        _ = read_json_representation(b'{"outer":{"same":1,"same":2}}')


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf{}",
        b"\xff",
        b"[]",
        b'{"value":NaN}',
        b"{} trailing",
        b"\x0b{}",
    ],
)
def test_json_parser_rejects_non_rfc_or_non_object_representations(raw: bytes) -> None:
    with pytest.raises(RepresentationProtocolError):
        _ = read_json_representation(raw)


def test_json_to_sse_preserves_unknowns_and_choice_order_while_replacing_message() -> None:
    parsed = read_json_representation(
        b"".join(
            (
                b'{"id":"result-1","object":"chat.completion","choices":[',
                b'{"index":1,"message":{"role":"assistant","content":"b"},',
                b'"finish_reason":null},',
                b'{"index":0,"message":{"role":"assistant","content":"a"},"extra":true}],',
                b'"usage":{},"provider_extension":"kept",',
                b'"large_integer":18446744073709551616}',
            )
        )
    )

    frames = json_to_sse_frames(parsed)

    assert len(frames) == 2
    assert frames[0].startswith(b"data: {")
    assert b'"object":"chat.completion.chunk"' in frames[0]
    assert b'"delta":{"role":"assistant","content":"b"}' in frames[0]
    assert frames[0].index(b'"index":1') < frames[0].index(b'"index":0')
    assert b'"provider_extension":"kept"' in frames[0]
    assert b'"large_integer":18446744073709551616' in frames[0]
    assert frames[1] == b"data: [DONE]\n\n"


@pytest.mark.parametrize(
    "raw",
    [
        b'{"id":1,"choices":[],"usage":{}}',
        b'{"id":"x","choices":{},"usage":{}}',
        b'{"id":"x","choices":[],"usage":[]}',
        b'{"id":"x","choices":[{"delta":{},"message":{}}],"usage":{}}',
        b'{"id":"x","choices":[{"message":[]}],"usage":{}}',
        b'{"id":"x","choices":[],"usage":{},"surrogate":"\\ud800"}',
        b'{"id":"x","choices":[],"usage":{},"overflow":1e400}',
    ],
)
def test_json_to_sse_requires_typed_fields_and_rejects_preexisting_delta(raw: bytes) -> None:
    with pytest.raises(RepresentationProtocolError):
        _ = json_to_sse_frames(read_json_representation(raw))


def test_deep_json_nesting_maps_recursion_to_protocol_failure() -> None:
    raw = b'{"deep":' + (b"[" * 2000) + b"0" + (b"]" * 2000) + b"}"

    with pytest.raises(RepresentationProtocolError):
        _ = read_json_representation(raw)


def test_oversized_integer_maps_conversion_limit_to_safe_protocol_failure() -> None:
    raw = b'{"id":"x","choices":[],"usage":{},"oversized_integer":' + (b"1" * 5000) + b"}"

    with pytest.raises(RepresentationProtocolError) as error:
        _ = read_json_representation(raw)

    assert str(error.value) == "upstream_representation_protocol_error"
    assert error.value.__cause__ is None
    assert error.value.__context__ is None


def test_invalid_utf8_representation_retains_no_decoder_exception_context() -> None:
    with pytest.raises(RepresentationProtocolError) as captured:
        _ = read_json_representation(b"\xff")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
