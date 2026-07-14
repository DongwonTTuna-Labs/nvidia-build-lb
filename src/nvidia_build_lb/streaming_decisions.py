"""Winner-based local send-error propagation decisions."""

from nvidia_build_lb.terminal import FrameReleaseDecision, TerminalProposal


def local_error_won_with_reraise(
    decision: FrameReleaseDecision,
    proposal: TerminalProposal,
) -> bool:
    """Reraise only when the same local error proposal is the durable winner."""
    terminal = decision.terminal
    if terminal is None:
        raise RuntimeError
    winner = terminal.winner
    return winner.kind is proposal.kind and winner.reraise
