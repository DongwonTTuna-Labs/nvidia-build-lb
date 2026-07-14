"""Ancestor-cancellation persistence for a live NVIDIA stream."""

import anyio

from nvidia_build_lb.streaming_control import (
    StreamContext,
    StreamState,
    coordinate_and_release,
)
from nvidia_build_lb.terminal import TerminalKind, TerminalProposal


async def _shielded_stop(
    context: StreamContext,
    state: StreamState,
    proposal: TerminalProposal,
) -> None:
    with anyio.CancelScope(shield=True):
        decision = await coordinate_and_release(
            context,
            proposal,
            generation=state.generation,
        )
        state.terminal_released = decision.terminal is not None


async def record_ancestor_cancellation(
    context: StreamContext,
    state: StreamState,
) -> None:
    """Persist cancellation unless terminal persistence already won."""
    if state.terminal_released:
        _ = context.routed.terminal.stream.trigger_pending_fail_stop()
        return
    proposal = TerminalProposal.for_kind(
        TerminalKind.ANCESTOR_CANCELLED,
        sequence=state.sequence,
    )
    try:
        await _shielded_stop(context, state, proposal)
    finally:
        _ = context.routed.terminal.stream.trigger_pending_fail_stop()
