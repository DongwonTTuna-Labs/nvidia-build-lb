"""Resolve live-stream close outcomes without replacing a selected terminal."""

import anyio

from nvidia_build_lb.response_retirement import ResponseRetirementUnresolvedError
from nvidia_build_lb.streaming_control import StreamContext, StreamState


async def close_stream_after_body(
    context: StreamContext,
    state: StreamState,
    primary_error: BaseException | None,
) -> None:
    """Retire once and preserve terminal or body primaries over ordinary close errors."""
    try:
        with anyio.CancelScope(shield=True):
            if state.terminal_released:
                await context.routed.terminal.stream.aclose_after_terminal()
            else:
                await context.routed.terminal.stream.aclose()
    except ResponseRetirementUnresolvedError:
        cancelled_type = anyio.get_cancelled_exc_class()
        if isinstance(primary_error, cancelled_type):
            _ = context.routed.terminal.stream.trigger_pending_fail_stop()
            raise
        if primary_error is None:
            raise
    except BaseException:
        if primary_error is None and not state.terminal_released:
            raise
