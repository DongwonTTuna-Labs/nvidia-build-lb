"""Secret-free fixed-target TCP relay for host-side candidate QA."""

from contextlib import suppress

import anyio
from anyio.abc import ByteStream

_BIND_HOST = "0.0.0.0"  # noqa: S104 - published on host loopback only.
_TARGET_HOST = "app"
_PORT = 2456
_BUFFER_BYTES = 65_536


async def _pipe(source: ByteStream, destination: ByteStream) -> None:
    try:
        while True:
            await destination.send(await source.receive(_BUFFER_BYTES))
    except (anyio.BrokenResourceError, anyio.ClosedResourceError, anyio.EndOfStream):
        with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await destination.send_eof()


async def _relay(client: ByteStream) -> None:
    try:
        upstream = await anyio.connect_tcp(_TARGET_HOST, _PORT)
    except OSError:
        await client.aclose()
        return
    async with client, upstream, anyio.create_task_group() as tasks:
        _ = tasks.start_soon(_pipe, client, upstream)
        _ = tasks.start_soon(_pipe, upstream, client)


async def _serve() -> None:
    listener = await anyio.create_tcp_listener(local_host=_BIND_HOST, local_port=_PORT)
    async with listener:
        await listener.serve(_relay)


if __name__ == "__main__":
    anyio.run(_serve)
