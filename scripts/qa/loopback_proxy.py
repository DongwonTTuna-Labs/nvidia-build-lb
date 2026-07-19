"""Secret-free fixed-target TCP relay for host-side candidate QA."""

import asyncio

_BIND_HOST = "0.0.0.0"  # noqa: S104 - published on host loopback only.
_TARGET_HOST = "app"
_PORT = 2456
_BUFFER_BYTES = 65_536


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(_BUFFER_BYTES):
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(_TARGET_HOST, _PORT)
    except OSError:
        writer.close()
        return
    await asyncio.gather(
        _pipe(reader, upstream_writer),
        _pipe(upstream_reader, writer),
        return_exceptions=True,
    )


async def _serve() -> None:
    server = await asyncio.start_server(_relay, _BIND_HOST, _PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_serve())
