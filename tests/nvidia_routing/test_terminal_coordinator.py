"""Real supervisor FIFO acceptance, cutoff, and reachable-pair arbitration."""

from datetime import UTC, datetime
from itertools import combinations
from uuid import uuid4

import pytest
from pydantic import SecretStr

from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptIdentity,
    AttemptLease,
    TerminalCommitted,
)
from nvidia_build_lb.terminal import (
    ChatSupervisor,
    ChatSupervisorDependencies,
    FrameReleaseKind,
    TerminalKind,
    TerminalProposal,
)

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_NOW = datetime(2026, 7, 13, tzinfo=UTC)
_CHECKPOINTS = (
    (
        TerminalKind.DEADLINE_EXPIRED,
        TerminalKind.EXPLICIT_DISCONNECT,
        TerminalKind.ANCESTOR_CANCELLED,
    ),
    (
        TerminalKind.DEADLINE_EXPIRED,
        TerminalKind.EXPLICIT_DISCONNECT,
        TerminalKind.ANCESTOR_CANCELLED,
        TerminalKind.SEND_CANCELLED,
        TerminalKind.SEND_OS_ERROR,
        TerminalKind.SEND_EXCEPTION,
    ),
    (
        TerminalKind.DEADLINE_EXPIRED,
        TerminalKind.EXPLICIT_DISCONNECT,
        TerminalKind.ANCESTOR_CANCELLED,
        TerminalKind.NETWORK_TERMINAL_FAILURE,
        TerminalKind.NETWORK_TERMINAL_SUCCESS,
    ),
)
_EXCLUSIVE = (
    frozenset(
        {
            TerminalKind.SEND_CANCELLED,
            TerminalKind.SEND_OS_ERROR,
            TerminalKind.SEND_EXCEPTION,
        }
    ),
    frozenset(
        {
            TerminalKind.NETWORK_TERMINAL_FAILURE,
            TerminalKind.NETWORK_TERMINAL_SUCCESS,
        }
    ),
)


def _reachable(first: TerminalKind, second: TerminalKind) -> bool:
    pair = frozenset({first, second})
    return any(first in domain and second in domain for domain in _CHECKPOINTS) and not any(
        pair <= group for group in _EXCLUSIVE
    )


_CASES = tuple(
    pytest.param(
        first,
        second,
        order,
        cutoff,
        id=f"{first.value}__{second.value}__{order}__{cutoff}",
    )
    for first, second in combinations(tuple(TerminalKind), 2)
    if _reachable(first, second)
    for order, cutoff in (
        ("AB", "both_before"),
        ("BA", "both_before"),
        ("AB", "A_before_B_after"),
        ("BA", "B_before_A_after"),
    )
)


class _Clock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 2.0


class _Attempts:
    commands: list[AttemptFinalizeCommand]

    def __init__(self) -> None:
        self.commands = []

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        self.commands.append(command)
        return TerminalCommitted(command.identity)


def _lease() -> AttemptLease:
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)
    return AttemptLease(identity, uuid4(), SecretStr("synthetic"))


@pytest.mark.parametrize(("first", "second", "order", "cutoff"), _CASES)
async def test_reachable_pair(
    first: TerminalKind,
    second: TerminalKind,
    order: str,
    cutoff: str,
) -> None:
    attempts = _Attempts()
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    lease = _lease()
    ordered = (first, second) if order == "AB" else (second, first)
    first_acceptance = await supervisor.coordinate(
        TerminalProposal.for_kind(ordered[0], sequence=1),
        generation=0,
    )
    if cutoff == "both_before":
        second_acceptance = await supervisor.coordinate(
            TerminalProposal.for_kind(ordered[1], sequence=2),
            generation=0,
        )
        release = await supervisor.release(lease=lease, generation=0, started_monotonic=1.0)
        expected_winner = first
        assert second_acceptance.accepted_generation == 0
        assert release.acceptance_cutoff == 2
    else:
        release = await supervisor.release(lease=lease, generation=0, started_monotonic=1.0)
        second_acceptance = await supervisor.coordinate(
            TerminalProposal.for_kind(ordered[1], sequence=2),
            generation=0,
        )
        expected_winner = ordered[0]
        assert second_acceptance.accepted_generation == 1
        assert release.acceptance_cutoff == 1

    assert first_acceptance.accepted_generation == 0
    assert release.kind is FrameReleaseKind.STOP
    assert release.terminal is not None
    assert release.terminal.winner.kind is expected_winner
    assert len(attempts.commands) == 1
