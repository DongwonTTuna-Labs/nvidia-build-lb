"""Safe live-stream terminal proposals for typed transport failures."""

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.asgi_response import sse_response_body, sse_response_start
from nvidia_build_lb.nvidia_types import NvidiaTransportError
from nvidia_build_lb.outcomes import TransportSignal, map_public_outcome
from nvidia_build_lb.stream_retirement_fail_stop import complete_before_stream_fail_stop
from nvidia_build_lb.streaming_control import (
    StreamContext,
    StreamState,
    coordinate_and_release,
    send_after_terminal,
)
from nvidia_build_lb.terminal import TerminalKind, TerminalProposal


def transport_failure_proposal(
    error: NvidiaTransportError,
    *,
    sequence: int,
    request_id: str,
) -> TerminalProposal:
    """Preserve the safe transport class while forbidding mid-stream replay."""
    outcome = map_public_outcome(
        TransportSignal(error.code, error.request_bytes_sent)
    ).without_alternate()
    return TerminalProposal.stream_failure(
        sequence=sequence,
        status_class=outcome.persisted_status or LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
        request_id=request_id,
    )


async def complete_network_failure(
    context: StreamContext,
    state: StreamState,
    proposal: TerminalProposal | None = None,
) -> None:
    """Persist one safe stream failure before its terminal error frame."""
    proposal = proposal or TerminalProposal.for_kind(
        TerminalKind.NETWORK_TERMINAL_FAILURE,
        sequence=state.sequence,
        request_id=context.routed.lease.identity.request_id,
    )
    generation = state.generation
    decision = await complete_before_stream_fail_stop(
        lambda: coordinate_and_release(context, proposal, generation=generation),
        context.routed.terminal.stream,
    )
    terminal = decision.terminal
    state.terminal_released = terminal is not None
    if terminal is None or terminal.winner.kind is not TerminalKind.NETWORK_TERMINAL_FAILURE:
        return
    if not state.response_started:
        await send_after_terminal(context, sse_response_start())
        state.response_started = True
    payload = terminal.winner.payload
    if payload is None:
        raise RuntimeError
    await send_after_terminal(context, sse_response_body(payload, more_body=False))
