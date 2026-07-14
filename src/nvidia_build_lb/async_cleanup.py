"""Bound cleanup calls even when a dependency shields its own cancellation."""

import asyncio  # noqa: TID251 - task detachment is required to bound inner cancellation shields.
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import override

_UNRESOLVED_TASKS: set[asyncio.Task[object]] = set()


class AsyncCleanupUnresolvedError(Exception):
    """A cleanup operation did not settle inside its hard observation budget."""

    @override
    def __str__(self) -> str:
        return "async_cleanup_unresolved"


async def run_bounded_cleanup[T](
    operation: Callable[[], Awaitable[T]],
    timeout_seconds: float,
) -> T:
    """Observe a detached same-loop cleanup without waiting on its inner shields."""
    if timeout_seconds <= 0:
        raise AsyncCleanupUnresolvedError
    result: list[T] = []

    async def invoke() -> None:
        result.append(await operation())

    task: asyncio.Task[None] = asyncio.create_task(invoke())
    try:
        done, _pending = await asyncio.wait((task,), timeout=timeout_seconds)
    except BaseException:
        _retain(task)
        raise
    if not done:
        _retain(task)
        raise AsyncCleanupUnresolvedError
    _ = task.result()
    return result[0]


def _retain(task: asyncio.Task[object]) -> None:
    """Keep an unresolved cleanup alive until process fail-stop or late completion."""
    _UNRESOLVED_TASKS.add(task)
    task.add_done_callback(_consume)


def _consume(task: asyncio.Task[object]) -> None:
    _UNRESOLVED_TASKS.discard(task)
    with suppress(BaseException):
        _ = task.result()
