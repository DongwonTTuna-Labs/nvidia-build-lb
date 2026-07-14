"""Preserve terminal-persistence primaries while enforcing stream fail-stop."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from nvidia_build_lb.response_retirement import ResponseRetirementUnresolvedError


class PendingRetirementStream(Protocol):
    """Expose one deferred process-fatal retirement signal."""

    def trigger_pending_fail_stop(self) -> bool:
        """Return whether this call triggered the pending fail-stop."""
        ...


async def complete_before_stream_fail_stop[T](
    operation: Callable[[], Awaitable[T]],
    stream: PendingRetirementStream,
) -> T:
    """Complete terminal persistence, then propagate unresolved retirement."""
    try:
        result = await operation()
    except BaseException:
        _ = stream.trigger_pending_fail_stop()
        raise
    if stream.trigger_pending_fail_stop():
        raise ResponseRetirementUnresolvedError from None
    return result
