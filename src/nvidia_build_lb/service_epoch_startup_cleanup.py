"""Hard-bounded best-effort cleanup for failed service-epoch startup."""

from collections.abc import Awaitable, Callable
from contextlib import suppress

import anyio

from nvidia_build_lb.async_cleanup import run_bounded_cleanup
from nvidia_build_lb.service_epoch_types import EpochSqlConnection


async def close_invalid_connection(
    connection: EpochSqlConnection,
    *,
    timeout_seconds: float,
) -> None:
    """Retire a rejected connection without trusting inner cancellation behavior."""
    await _best_effort(connection.close, timeout_seconds)


async def cleanup_failed_start(
    connection: EpochSqlConnection,
    *,
    locked: bool,
    first_lock_key: int,
    second_lock_key: int,
    timeout_seconds: float,
) -> None:
    """Attempt bounded unlock then bounded close while preserving the startup primary."""
    if locked:
        await _best_effort(
            lambda: connection.advisory_unlock(first_lock_key, second_lock_key),
            timeout_seconds,
        )
    await _best_effort(connection.close, timeout_seconds)


async def stop_startup_monitor(
    scope: anyio.CancelScope,
    joined: anyio.Event,
    *,
    timeout_seconds: float,
) -> None:
    """Bound monitor unwind before releasing a failed startup connection."""
    scope.cancel()
    with suppress(BaseException):
        with anyio.CancelScope(shield=True):
            with anyio.fail_after(timeout_seconds):
                await joined.wait()


async def _best_effort[T](
    operation: Callable[[], Awaitable[T]],
    timeout_seconds: float,
) -> None:
    """Run one zero-argument async callable inside a detached hard bound."""
    with suppress(BaseException):
        with anyio.CancelScope(shield=True):
            _ = await run_bounded_cleanup(operation, timeout_seconds)
