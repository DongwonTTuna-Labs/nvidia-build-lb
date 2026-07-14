"""Fatal transition after a selected terminal becomes durable."""

from typing import Protocol

from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.response_retirement import ResponseRetirementUnresolvedError


class TerminalRetirement(Protocol):
    """Expose whether physical retirement remained unresolved."""

    @property
    def retirement_unresolved(self) -> bool:
        """Return the safe fatal-cleanup signal."""
        ...


class FailStopDependencies(Protocol):
    """Expose the process fail-stop owned by routing."""

    @property
    def fail_stop(self) -> AttemptFailStop:
        """Return the process lifecycle fail-stop."""
        ...


def fail_stop_after_terminal(
    dependencies: FailStopDependencies,
    terminal: TerminalRetirement,
) -> None:
    """Always propagate fatal retirement after requesting process cancellation."""
    if not terminal.retirement_unresolved:
        return
    dependencies.fail_stop.trigger()
    raise ResponseRetirementUnresolvedError from None
