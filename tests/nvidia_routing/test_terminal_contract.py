"""Terminal priority, deterministic duplicates, and one durable CAS."""

from datetime import UTC, datetime
from itertools import combinations
from uuid import uuid4

import pytest
from pydantic import SecretStr

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptIdentity,
    AttemptLease,
    TerminalCommitted,
)
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.terminal import (
    ChatSupervisor,
    ChatSupervisorDependencies,
    FrameReleaseKind,
    TerminalArbiter,
    TerminalKind,
    TerminalProposal,
)

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_NOW = datetime(2026, 7, 13, tzinfo=UTC)


def _proposal(kind: TerminalKind, sequence: int) -> TerminalProposal:
    return TerminalProposal.for_kind(kind, sequence=sequence)


def test_every_reachable_pair_uses_fixed_priority_independent_of_input_order() -> None:
    kinds = tuple(TerminalKind)
    for first, second in combinations(kinds, 2):
        forward = TerminalArbiter().register_batch((_proposal(first, 2), _proposal(second, 1)))
        reverse = TerminalArbiter().register_batch((_proposal(second, 1), _proposal(first, 2)))

        assert forward.winner.kind is first
        assert reverse.winner.kind is first
        assert forward.cas_success is True
        assert reverse.cas_success is True


def test_same_discriminator_selects_lexicographically_least_canonical_tuple() -> None:
    high = TerminalProposal.network_failure(
        sequence=2,
        status_class=LastStatusClass.UPSTREAM_UNAVAILABLE,
        payload=b"z",
    )
    low = TerminalProposal.network_failure(
        sequence=1,
        status_class=LastStatusClass.UPSTREAM_UNAVAILABLE,
        payload=b"a",
    )

    decision = TerminalArbiter().register_batch((high, low, high))

    assert decision.winner == low
    assert decision.discarded_count == 2


class _Clock:
    def now(self) -> datetime:
        return _NOW

    def monotonic(self) -> float:
        return 3.0


class _Attempts:
    commands: list[AttemptFinalizeCommand]

    def __init__(self) -> None:
        self.commands = []

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        self.commands.append(command)
        return TerminalCommitted(command.identity)


async def test_supervisor_persists_one_winner_and_repeated_loser_has_zero_delta() -> None:
    attempts = _Attempts()
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)
    lease = AttemptLease(identity, uuid4(), SecretStr("synthetic"))
    network = TerminalProposal.network_success(sequence=1, payload=b"data: [DONE]\n\n")
    disconnect = TerminalProposal.for_kind(TerminalKind.EXPLICIT_DISCONNECT, sequence=2)

    first = await supervisor.finalize(
        lease=lease,
        proposals=(network,),
        started_monotonic=1.0,
    )
    repeated = await supervisor.finalize(
        lease=lease,
        proposals=(disconnect,),
        started_monotonic=1.0,
    )

    assert first.cas_success is True
    assert repeated.cas_success is False
    assert repeated.winner == network
    assert len(attempts.commands) == 1
    assert attempts.commands[0].outcome is TerminalOutcome.SUCCEEDED
    assert attempts.commands[0].status_class is LastStatusClass.SUCCESS


async def test_release_linearization_routes_late_proposal_to_next_generation() -> None:
    attempts = _Attempts()
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)
    lease = AttemptLease(identity, uuid4(), SecretStr("synthetic"))

    first = await supervisor.release(lease=lease, generation=0, started_monotonic=1.0)
    acceptance = await supervisor.coordinate(
        _proposal(TerminalKind.EXPLICIT_DISCONNECT, 1),
        generation=0,
    )
    second = await supervisor.release(lease=lease, generation=1, started_monotonic=1.0)

    assert first.kind is FrameReleaseKind.CONTINUE
    assert acceptance.accepted_generation == 1
    assert second.kind is FrameReleaseKind.STOP
    assert second.terminal is not None
    assert second.terminal.winner.kind is TerminalKind.EXPLICIT_DISCONNECT
    assert len(attempts.commands) == 1


async def test_release_batches_all_accepted_proposals_before_fixed_priority_stop() -> None:
    attempts = _Attempts()
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)
    lease = AttemptLease(identity, uuid4(), SecretStr("synthetic"))
    _ = await supervisor.coordinate(
        _proposal(TerminalKind.NETWORK_TERMINAL_SUCCESS, 2),
        generation=0,
    )
    _ = await supervisor.coordinate(
        _proposal(TerminalKind.DEADLINE_EXPIRED, 1),
        generation=0,
    )

    release = await supervisor.release(lease=lease, generation=0, started_monotonic=1.0)

    assert release.kind is FrameReleaseKind.STOP
    assert release.terminal is not None
    assert release.terminal.winner.kind is TerminalKind.DEADLINE_EXPIRED
    assert release.acceptance_cutoff == 2
    assert len(attempts.commands) == 1
