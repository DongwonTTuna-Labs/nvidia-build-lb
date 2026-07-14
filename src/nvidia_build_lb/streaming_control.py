"""Supervisor coordination for ASGI send outcomes and disconnects."""

from dataclasses import dataclass
from typing import Protocol

import anyio

from nvidia_build_lb.asgi_response import AsgiMessage, AsgiReceive, AsgiSend
from nvidia_build_lb.routing import RoutedStream
from nvidia_build_lb.streaming_decisions import local_error_won_with_reraise
from nvidia_build_lb.terminal import (
    ChatSupervisor,
    FrameReleaseDecision,
    TerminalKind,
    TerminalProposal,
)


class DownstreamSendOSError(Exception):
    """Stop after a persisted downstream operating-system failure."""


class DownstreamSendError(Exception):
    """Carry the original generic downstream exception without rendering it."""

    original: Exception

    def __init__(self, original: Exception) -> None:
        """Retain only the original exception object for exact re-raise."""
        super().__init__()
        self.original = original


class DownstreamDisconnectedError(Exception):
    """Stop a pending upstream read or downstream send after explicit disconnect."""


class DownstreamTerminalStopError(Exception):
    """Stop locally when a non-reraise durable terminal defeated the send error."""


class DisconnectEvent(Protocol):
    """Read-only event seam used by send and upstream-read races."""

    def is_set(self) -> bool:
        """Return whether disconnect already won."""
        ...

    async def wait(self) -> None:
        """Wait until disconnect wins."""
        ...


@dataclass(frozen=True, slots=True)
class StreamContext:
    """Immutable live stream dependencies."""

    routed: RoutedStream
    send: AsgiSend
    supervisor: ChatSupervisor
    disconnected: DisconnectEvent


@dataclass(slots=True)
class StreamState:
    """Mutable response and release generation state."""

    response_started: bool = False
    sequence: int = 0
    generation: int = 0
    terminal_released: bool = False


@dataclass(slots=True)
class _SendAttempt:
    settled: bool = False
    error: Exception | None = None


async def watch_disconnect(
    receive: AsgiReceive,
    supervisor: ChatSupervisor,
    disconnected: anyio.Event,
) -> None:
    """Submit one explicit disconnect to the earliest open generation."""
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            _ = await supervisor.coordinate(
                TerminalProposal.for_kind(TerminalKind.EXPLICIT_DISCONNECT, sequence=0),
                generation=0,
            )
            disconnected.set()
            return


async def send_or_stop(
    context: StreamContext,
    message: AsgiMessage,
    state: StreamState,
) -> None:
    """Classify one ASGI send outcome without upstream reclassification."""
    cancelled_type = anyio.get_cancelled_exc_class()
    try:
        error = await _send_or_disconnect(context, message)
    except cancelled_type:
        with anyio.CancelScope(shield=True):
            _ = await context.supervisor.coordinate(
                TerminalProposal.for_kind(
                    TerminalKind.SEND_CANCELLED,
                    sequence=state.sequence,
                ),
                generation=state.generation,
            )
        raise
    if error is None:
        return
    if isinstance(error, OSError):
        proposal = TerminalProposal.for_kind(
            TerminalKind.SEND_OS_ERROR,
            sequence=state.sequence,
        )
        decision = await coordinate_and_release(
            context,
            proposal,
            generation=state.generation,
        )
        state.terminal_released = decision.terminal is not None
        if not local_error_won_with_reraise(decision, proposal):
            raise DownstreamTerminalStopError from None
        raise DownstreamSendOSError from None
    proposal = TerminalProposal.for_kind(
        TerminalKind.SEND_EXCEPTION,
        sequence=state.sequence,
    )
    decision = await coordinate_and_release(
        context,
        proposal,
        generation=state.generation,
    )
    state.terminal_released = decision.terminal is not None
    if not local_error_won_with_reraise(decision, proposal):
        raise DownstreamTerminalStopError from None
    raise DownstreamSendError(error) from None


async def send_after_terminal(context: StreamContext, message: AsgiMessage) -> None:
    """Send after terminal persistence without proposing a second terminal class."""
    error = await _send_or_disconnect(context, message)
    if error is None:
        return
    if isinstance(error, OSError):
        raise DownstreamSendOSError from None
    raise DownstreamSendError(error) from None


async def _send_or_disconnect(
    context: StreamContext,
    message: AsgiMessage,
) -> Exception | None:
    """Cancel one pending ASGI send when the disconnect watcher wins."""
    if context.disconnected.is_set():
        raise DownstreamDisconnectedError
    attempt = _SendAttempt()
    ready = anyio.Event()
    cancelled_type = anyio.get_cancelled_exc_class()

    async def send_message() -> None:
        try:
            if context.disconnected.is_set():
                return
            await context.send(message)
        except cancelled_type:
            raise
        except Exception as error:  # noqa: BLE001 - returned to the closed classifier.
            attempt.error = error
            attempt.settled = True
        else:
            attempt.settled = True
        finally:
            ready.set()

    async def wait_disconnect() -> None:
        await context.disconnected.wait()
        ready.set()

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(send_message)
        _ = tasks.start_soon(wait_disconnect)
        await ready.wait()
        tasks.cancel_scope.cancel()
    if not attempt.settled:
        raise DownstreamDisconnectedError
    return attempt.error


async def coordinate_and_release(
    context: StreamContext,
    proposal: TerminalProposal,
    *,
    generation: int,
) -> FrameReleaseDecision:
    """Accept one proposal and close its generation exactly once."""
    _ = await context.supervisor.coordinate(proposal, generation=generation)
    return await context.supervisor.release(
        lease=context.routed.lease,
        generation=generation,
        started_monotonic=context.routed.started_monotonic,
    )


async def release(context: StreamContext, state: StreamState) -> FrameReleaseDecision:
    """Commit the current generation release decision."""
    decision = await context.supervisor.release(
        lease=context.routed.lease,
        generation=state.generation,
        started_monotonic=context.routed.started_monotonic,
    )
    state.terminal_released = decision.terminal is not None
    return decision
