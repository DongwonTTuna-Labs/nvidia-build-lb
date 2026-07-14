"""Single monotonic polling deadline and clipped await contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nvidia_build_lb.polling import PollDeadlineExpiredError, PollingDeadline

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _ManualMonotonic:
    value: float

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value


class _Operation:
    clock: _ManualMonotonic
    advance: float
    observed_budgets: list[float]

    def __init__(self, clock: _ManualMonotonic, advance: float) -> None:
        self.clock = clock
        self.advance = advance
        self.observed_budgets = []

    async def __call__(self, remaining: float) -> str:
        self.observed_budgets.append(remaining)
        self.clock.value += self.advance
        return "complete"


async def test_deadline_recomputes_before_and_after_await_and_wins_at_equality() -> None:
    clock = _ManualMonotonic()
    deadline = PollingDeadline(clock=clock, expires_at=2.0)
    operation = _Operation(clock, advance=2.0)

    with pytest.raises(PollDeadlineExpiredError):
        _ = await deadline.run(operation)

    assert operation.observed_budgets == [2.0]


async def test_deadline_passes_only_current_remaining_budget() -> None:
    clock = _ManualMonotonic(value=1.25)
    deadline = PollingDeadline(clock=clock, expires_at=2.0)
    operation = _Operation(clock, advance=0.25)

    result = await deadline.run(operation)

    assert result == "complete"
    assert operation.observed_budgets == [0.75]


def test_deadline_operation_shape_is_one_budgeted_awaitable() -> None:
    async def operation(_remaining: float) -> int:
        return 1

    typed: Callable[[float], Awaitable[int]] = operation

    assert callable(typed)
