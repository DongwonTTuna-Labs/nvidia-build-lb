"""Safe typed failure for an admin probe that never acquired an attempt."""

from nvidia_build_lb.poll_terminal_types import FailureTerminal


class ProbeNotExecutedError(Exception):
    """Carry one closed routed failure when no durable probe terminal exists."""

    terminal: FailureTerminal

    def __init__(self, terminal: FailureTerminal) -> None:
        """Retain only the already-sanitized routed terminal."""
        super().__init__()
        self.terminal = terminal
