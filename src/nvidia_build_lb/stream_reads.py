"""Interruptible upstream frame reads and disconnect release."""

from collections.abc import AsyncIterator
from dataclasses import dataclass

import anyio

from nvidia_build_lb.sse import SSEFrame
from nvidia_build_lb.stream_retirement_fail_stop import complete_before_stream_fail_stop
from nvidia_build_lb.streaming_control import (
    DisconnectEvent,
    StreamContext,
    StreamState,
    release,
)
from nvidia_build_lb.terminal import FrameReleaseKind


@dataclass(slots=True)
class _FrameRead:
    frame: SSEFrame | None = None
    exhausted: bool = False
    error: Exception | None = None


async def next_frame_or_disconnect(
    iterator: AsyncIterator[SSEFrame],
    disconnected: DisconnectEvent,
) -> tuple[bool, SSEFrame | None]:
    """Race one upstream read against the explicit-disconnect event."""
    if disconnected.is_set():
        return True, None
    state = _FrameRead()
    ready = anyio.Event()
    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(_read_frame, iterator, disconnected, state, ready)
        _ = tasks.start_soon(_wait_disconnect, disconnected, ready)
        await ready.wait()
        tasks.cancel_scope.cancel()
    if disconnected.is_set():
        return True, None
    if state.error is not None:
        raise state.error
    if state.exhausted:
        return False, None
    if state.frame is None:
        raise RuntimeError
    return False, state.frame


async def _read_frame(
    iterator: AsyncIterator[SSEFrame],
    disconnected: DisconnectEvent,
    state: _FrameRead,
    ready: anyio.Event,
) -> None:
    """Recheck disconnect in the scheduled task immediately before advancing."""
    try:
        if disconnected.is_set():
            return
        state.frame = await iterator.__anext__()
    except StopAsyncIteration:
        state.exhausted = True
    except anyio.get_cancelled_exc_class():
        raise
    except Exception as error:  # noqa: BLE001 - re-raised at the closed boundary.
        state.error = error
    finally:
        ready.set()


async def _wait_disconnect(disconnected: DisconnectEvent, ready: anyio.Event) -> None:
    await disconnected.wait()
    ready.set()


async def finish_disconnect(context: StreamContext, state: StreamState) -> None:
    """Persist the already accepted disconnect at the release boundary."""
    decision = await complete_before_stream_fail_stop(
        lambda: release(context, state),
        context.routed.terminal.stream,
    )
    if decision.kind is not FrameReleaseKind.STOP:
        raise RuntimeError
