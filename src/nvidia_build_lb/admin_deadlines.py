"""Closed safe classifications and runner for bounded administration reads."""

from collections.abc import Awaitable, Callable

import anyio


class AdminReadTimeoutError(RuntimeError):
    """The current dashboard snapshot was not confirmed in time."""


class AdminMutationTimeoutError(RuntimeError):
    """The mutation outcome is unknown after server deadline expiry."""


class AdminMutationResponseInvalidError(RuntimeError):
    """The mutation completed without one bounded strict response."""


class AdminMutationSettlingError(RuntimeError):
    """No dashboard snapshot can yet reconcile an active/recent mutation."""


class RuntimeUnavailableError(RuntimeError):
    """Lifecycle readiness was withdrawn before mutation domain admission."""


async def run_admin_read[T](operation: Callable[[], Awaitable[T]], seconds: float) -> T:
    """Run one supported admin read under the shared server deadline."""
    try:
        with anyio.fail_after(seconds):
            return await operation()
    except TimeoutError:
        raise AdminReadTimeoutError from None
