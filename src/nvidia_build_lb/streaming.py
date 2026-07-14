"""ASGI JSON/SSE response sequencing with terminal persistence boundaries."""

from collections.abc import AsyncIterator

import anyio

from nvidia_build_lb.asgi_response import (
    AsgiReceive,
    AsgiSend,
    send_json_response,
    sse_response_body,
    sse_response_start,
)
from nvidia_build_lb.nvidia_types import NvidiaTransportError
from nvidia_build_lb.representations import RepresentationProtocolError
from nvidia_build_lb.routing import RoutedJson, RoutedStream
from nvidia_build_lb.sse import SSEFrame
from nvidia_build_lb.stream_cancellation import record_ancestor_cancellation
from nvidia_build_lb.stream_close import close_stream_after_body
from nvidia_build_lb.stream_failures import transport_failure_proposal
from nvidia_build_lb.stream_reads import finish_disconnect, next_frame_or_disconnect
from nvidia_build_lb.stream_retirement_fail_stop import complete_before_stream_fail_stop
from nvidia_build_lb.streaming_control import (
    DownstreamDisconnectedError,
    DownstreamSendError,
    DownstreamSendOSError,
    DownstreamTerminalStopError,
    StreamContext,
    StreamState,
    coordinate_and_release,
    release,
    send_after_terminal,
    send_or_stop,
    watch_disconnect,
)
from nvidia_build_lb.terminal import (
    ChatSupervisor,
    FrameReleaseKind,
    TerminalKind,
    TerminalProposal,
)


class ChatStreamResponder:
    """Emit one response start and enforce persistence before terminal output."""

    _supervisor: ChatSupervisor | None

    def __init__(self, supervisor: ChatSupervisor | None) -> None:
        """Bind a stream supervisor, or none for already-finalized JSON."""
        self._supervisor = supervisor

    async def run(
        self,
        *,
        routed: RoutedJson | RoutedStream,
        receive: AsgiReceive,
        send: AsgiSend,
        requested_stream: bool,
    ) -> None:
        """Dispatch one routed terminal representation to its exact ASGI path."""
        if isinstance(routed, RoutedJson):
            await self.run_json(routed=routed, send=send, requested_stream=requested_stream)
            return
        await self.run_stream(routed=routed, receive=receive, send=send)

    async def run_json(
        self,
        *,
        routed: RoutedJson,
        send: AsgiSend,
        requested_stream: bool,
    ) -> None:
        """Send committed JSON unchanged or convert it to data plus DONE SSE."""
        await send_json_response(routed, send, requested_stream=requested_stream)

    async def run_stream(
        self,
        *,
        routed: RoutedStream,
        receive: AsgiReceive,
        send: AsgiSend,
    ) -> None:
        """Forward ordinary frames and arbitrate exactly one terminal frame."""
        supervisor = self._supervisor
        if supervisor is None:
            raise RuntimeError
        body_error: Exception | None = None
        disconnected = anyio.Event()
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(watch_disconnect, receive, supervisor, disconnected)
            try:
                try:
                    await self._run_stream_body(
                        StreamContext(routed, send, supervisor, disconnected)
                    )
                except DownstreamSendError as failure:
                    body_error = failure.original
                except Exception as failure:  # noqa: BLE001 - re-raised outside the task group.
                    body_error = failure
            finally:
                tasks.cancel_scope.cancel()
        if body_error is not None:
            _ = routed.terminal.stream.trigger_pending_fail_stop()
            raise body_error from None

    async def _run_stream_body(self, context: StreamContext) -> None:
        state = StreamState()
        cancelled_type = anyio.get_cancelled_exc_class()
        iterator = context.routed.terminal.stream.frames().__aiter__()
        primary_error: BaseException | None = None
        try:
            await self._forward_frames(context, state, iterator)
        except DownstreamDisconnectedError:
            try:
                await finish_disconnect(context, state)
            except BaseException as error:
                primary_error = error
                raise
        except DownstreamSendOSError:
            return
        except DownstreamTerminalStopError:
            return
        except DownstreamSendError as error:
            primary_error = error
            raise
        except cancelled_type as error:
            primary_error = error
            await record_ancestor_cancellation(context, state)
            raise
        except BaseException as error:
            primary_error = error
            raise
        finally:
            await close_stream_after_body(context, state, primary_error)

    async def _forward_frames(
        self,
        context: StreamContext,
        state: StreamState,
        iterator: AsyncIterator[SSEFrame],
    ) -> None:
        while True:
            observed = await self._read_or_fail(context, state, iterator)
            if observed is None:
                return
            disconnected, frame = observed
            if disconnected:
                await finish_disconnect(context, state)
                return
            if frame is None or await self._handle_frame(context, state, frame):
                return

    async def _read_or_fail(
        self,
        context: StreamContext,
        state: StreamState,
        iterator: AsyncIterator[SSEFrame],
    ) -> tuple[bool, SSEFrame | None] | None:
        try:
            return await next_frame_or_disconnect(iterator, context.disconnected)
        except NvidiaTransportError as error:
            proposal = transport_failure_proposal(
                error,
                sequence=state.sequence,
                request_id=context.routed.lease.identity.request_id,
            )
        except (OSError, RepresentationProtocolError):
            proposal = None
        except Exception:  # noqa: BLE001 - provider text is never inspected.
            proposal = None
        await self._network_failure(context, state, proposal)
        return None

    async def _handle_frame(
        self,
        context: StreamContext,
        state: StreamState,
        frame: SSEFrame,
    ) -> bool:
        if await context.supervisor.has_pending(generation=state.generation):
            decision = await release(context, state)
            if decision.kind is FrameReleaseKind.STOP:
                return True
        if frame.done:
            await self._publish_done(context, state, frame)
            return True
        if not state.response_started:
            await send_or_stop(context, sse_response_start(), state)
            state.response_started = True
        state.sequence += 1
        await send_or_stop(context, sse_response_body(frame.raw, more_body=True), state)
        decision = await release(context, state)
        if decision.kind is FrameReleaseKind.STOP:
            return True
        state.generation += 1
        return False

    async def _publish_done(
        self,
        context: StreamContext,
        state: StreamState,
        frame: SSEFrame,
    ) -> None:
        decision = await coordinate_and_release(
            context,
            TerminalProposal.network_success(sequence=state.sequence, payload=frame.raw),
            generation=state.generation,
        )
        terminal = decision.terminal
        state.terminal_released = terminal is not None
        if terminal is None or terminal.winner.kind is not TerminalKind.NETWORK_TERMINAL_SUCCESS:
            return
        if not state.response_started:
            await send_after_terminal(context, sse_response_start())
            state.response_started = True
        await send_after_terminal(context, sse_response_body(frame.raw, more_body=False))

    async def _network_failure(
        self,
        context: StreamContext,
        state: StreamState,
        proposal: TerminalProposal | None = None,
    ) -> None:
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
