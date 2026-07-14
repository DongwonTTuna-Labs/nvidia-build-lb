"""Bounded raw-representation consumption without convenience decoders."""

from collections.abc import AsyncIterator

import pytest

from nvidia_build_lb.representations import (
    BoundedResponseReader,
    RepresentationProtocolError,
)

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _RawChunks:
    chunks: tuple[bytes, ...]
    iterator_calls: int

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self.chunks = chunks
        self.iterator_calls = 0

    async def aiter_raw(self) -> AsyncIterator[bytes]:
        self.iterator_calls += 1
        for chunk in self.chunks:
            yield chunk


async def test_reader_uses_one_raw_iterator_and_preserves_representation_bytes() -> None:
    source = _RawChunks((b'{"choices":', b"[]}"))
    reader = BoundedResponseReader(max_bytes=64, expected_content_length=14)

    observed = await reader.read(source)

    assert observed == b'{"choices":[]}'
    assert source.iterator_calls == 1


async def test_reader_enforces_bound_before_buffer_growth() -> None:
    source = _RawChunks((b"1234", b"5678"))
    reader = BoundedResponseReader(max_bytes=7, expected_content_length=None)

    with pytest.raises(RepresentationProtocolError):
        _ = await reader.read(source)


async def test_reader_checks_content_length_only_at_full_eof() -> None:
    source = _RawChunks((b"123",))
    reader = BoundedResponseReader(max_bytes=8, expected_content_length=4)

    with pytest.raises(RepresentationProtocolError):
        _ = await reader.read(source)
