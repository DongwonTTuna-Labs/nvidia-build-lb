"""Deadline, dependency, and terminal types for NVIDIA polling."""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol, Self, override

import anyio

from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.headers import ValidatedUpstreamHeaders
from nvidia_build_lb.nvidia_types import NvidiaClient, NvidiaResponse
from nvidia_build_lb.poll_terminal_types import FailureTerminal, JsonTerminal
from nvidia_build_lb.representations import MAX_SSE_FRAME_BYTES
from nvidia_build_lb.response_retirement import (
    ResponseRetirementUnresolvedError,
    retire_response,
)
from nvidia_build_lb.sse import SSEFrame, SSEFrameParser, SSEProtocolError

_POLL_DEADLINE_ERROR = "upstream_poll_deadline"


class MonotonicClock(Protocol):
    """Supply monotonic seconds for one non-resetting deadline."""

    def monotonic(self) -> float:
        """Return the current monotonic time."""
        ...


class Sleeper(Protocol):
    """Await a clipped polling delay."""

    async def sleep(self, seconds: float) -> None:
        """Sleep for the requested nonnegative duration."""
        ...


class JitterSource(Protocol):
    """Choose one equal-jitter value inside a closed range."""

    def uniform(self, lower: float, upper: float) -> float:
        """Return one value in the inclusive range."""
        ...


class PollDeadlineExpiredError(Exception):
    """The one origin-owned monotonic deadline won arbitration."""

    @override
    def __str__(self) -> str:
        return _POLL_DEADLINE_ERROR


@dataclass(frozen=True, slots=True)
class PollingDeadline:
    """Run every poll await inside one absolute monotonic budget."""

    clock: MonotonicClock
    expires_at: float

    def remaining(self) -> float:
        """Return positive remaining time or fail at the deadline."""
        remaining = self.expires_at - self.clock.monotonic()
        if remaining <= 0:
            raise PollDeadlineExpiredError
        return remaining

    async def run[T](self, operation: Callable[[float], Awaitable[T]]) -> T:
        """Recompute around one await and let deadline equality win."""
        remaining = self.remaining()
        try:
            with anyio.fail_after(remaining):
                result = await operation(remaining)
        except TimeoutError:
            raise PollDeadlineExpiredError from None
        _ = self.remaining()
        return result


@dataclass(frozen=True, slots=True)
class PollDependencies:
    """All time, I/O, and deterministic jitter dependencies."""

    client: NvidiaClient
    monotonic_clock: MonotonicClock
    wall_clock: Clock
    sleeper: Sleeper
    jitter: JitterSource
    fail_stop: AttemptFailStop


@dataclass(frozen=True, slots=True)
class PrimedSSEState:
    """Parser state acquired before a live SSE stream is handed off."""

    parser: SSEFrameParser
    initial: tuple[SSEFrame, ...]
    expected_content_length: int | None


class NvidiaSSEStream:
    """Continue one already-primed raw iterator and own response closure."""

    __slots__: ClassVar[tuple[str, ...]] = (
        "_close_lock",
        "_closed",
        "_expected_content_length",
        "_fail_stop",
        "_initial",
        "_iterator",
        "_parser",
        "_pending_fail_stop",
        "_response",
    )
    _close_lock: anyio.Lock
    _closed: bool
    _expected_content_length: int | None
    _fail_stop: AttemptFailStop
    _initial: tuple[SSEFrame, ...]
    _iterator: AsyncIterator[bytes]
    _parser: SSEFrameParser
    _pending_fail_stop: bool
    _response: NvidiaResponse

    def __init__(
        self,
        *,
        response: NvidiaResponse,
        iterator: AsyncIterator[bytes],
        state: PrimedSSEState,
        fail_stop: AttemptFailStop,
    ) -> None:
        """Take ownership of one response and its raw iterator."""
        self._response = response
        self._iterator = iterator
        self._parser = state.parser
        self._initial = state.initial
        self._expected_content_length = state.expected_content_length
        self._fail_stop = fail_stop
        self._close_lock = anyio.Lock()
        self._closed = False
        self._pending_fail_stop = False

    @classmethod
    async def prime(
        cls,
        response: NvidiaResponse,
        headers: ValidatedUpstreamHeaders,
        deadline: PollingDeadline,
        fail_stop: AttemptFailStop,
    ) -> Self:
        """Acquire the first complete frame inside the deadline."""
        iterator = response.aiter_raw()
        parser = SSEFrameParser(max_frame_bytes=MAX_SSE_FRAME_BYTES)
        frames: tuple[SSEFrame, ...] = ()
        while not frames:
            try:
                chunk = await deadline.run(lambda _remaining: iterator.__anext__())
            except StopAsyncIteration:
                parser.finish(expected_content_length=headers.content_length)
                raise SSEProtocolError from None
            frames = parser.feed(chunk)
        return cls(
            response=response,
            iterator=iterator,
            state=PrimedSSEState(parser, frames, headers.content_length),
            fail_stop=fail_stop,
        )

    async def frames(self) -> AsyncIterator[SSEFrame]:
        """Yield complete frames without advancing during consumer wait."""
        primary_error: BaseException | None = None
        try:
            for frame in self._initial:
                yield frame
            while not self._parser.done:
                pending = self._parser.feed(b"")
                if pending:
                    yield pending[0]
                    continue
                try:
                    chunk = await self._iterator.__anext__()
                except StopAsyncIteration:
                    self._parser.finish(expected_content_length=self._expected_content_length)
                    return
                frames = self._parser.feed(chunk)
                if frames:
                    yield frames[0]
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                await self._close(defer_fail_stop=primary_error is not None)
            except BaseException:
                if primary_error is None:
                    raise

    async def aclose(self) -> None:
        """Close the owned response at most once."""
        await self._close(defer_fail_stop=False)

    async def aclose_after_terminal(self) -> None:
        """Close after terminal persistence while deferring process fail-stop."""
        await self._close(defer_fail_stop=True)

    async def _close(self, *, defer_fail_stop: bool) -> None:
        with anyio.CancelScope(shield=True):
            async with self._close_lock:
                if self._closed:
                    return
                try:
                    await retire_response(self._response)
                except ResponseRetirementUnresolvedError:
                    self._closed = True
                    if defer_fail_stop:
                        self._pending_fail_stop = True
                    else:
                        self._fail_stop.trigger()
                    raise
                except BaseException:
                    self._closed = True
                    raise
                else:
                    self._closed = True

    def trigger_pending_fail_stop(self) -> bool:
        """Trigger a deferred fatal transition only after terminal persistence."""
        if not self._pending_fail_stop:
            return False
        self._pending_fail_stop = False
        self._fail_stop.trigger()
        return True


@dataclass(frozen=True, slots=True)
class StreamTerminal:
    """A strict SSE terminal primed inside the polling deadline."""

    stream: NvidiaSSEStream
    headers: ValidatedUpstreamHeaders


type PollTerminal = JsonTerminal | StreamTerminal | FailureTerminal
