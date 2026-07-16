"""Supervisor-owned terminal persistence and frame-release linearization."""

from dataclasses import dataclass
from typing import Protocol

import anyio

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.attempt_types import AttemptFinalizeCommand, AttemptLease, TerminalCommitted
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.terminal_arbiter import (
    FrameReleaseDecision,
    FrameReleaseKind,
    ProposalAcceptance,
    TerminalArbiter,
    TerminalDecision,
    TerminalKind,
    TerminalProposal,
)

__all__ = [
    "ChatSupervisor",
    "ChatSupervisorDependencies",
    "FrameReleaseDecision",
    "FrameReleaseKind",
    "ProposalAcceptance",
    "TerminalArbiter",
    "TerminalDecision",
    "TerminalKind",
    "TerminalProposal",
]


class TerminalAttemptStore(Protocol):
    """Durably finalize one already reserved attempt."""

    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        """Commit exactly one terminal receipt and event."""
        ...


class MonotonicClock(Protocol):
    """Supply monotonic seconds for latency."""

    def monotonic(self) -> float:
        """Return current monotonic seconds."""
        ...


@dataclass(frozen=True, slots=True)
class ChatSupervisorDependencies:
    """Attempt store plus wall and monotonic clocks."""

    attempts: TerminalAttemptStore
    clock: Clock
    monotonic_clock: MonotonicClock


class ChatSupervisor:
    """Serialize proposal acceptance, release decisions, CAS, and DB commit."""

    _arbiter: TerminalArbiter
    _dependencies: ChatSupervisorDependencies
    _generation_proposals: dict[int, list[TerminalProposal]]
    _release_decisions: dict[int, FrameReleaseDecision]
    _acceptance_sequence: int
    _persisted_status_class: LastStatusClass | None
    _lock: anyio.Lock

    def __init__(self, dependencies: ChatSupervisorDependencies) -> None:
        """Create one supervisor for one live routed stream."""
        self._dependencies = dependencies
        self._arbiter = TerminalArbiter()
        self._generation_proposals = {}
        self._release_decisions = {}
        self._acceptance_sequence = 0
        self._persisted_status_class = None
        self._lock = anyio.Lock()

    async def coordinate(
        self,
        proposal: TerminalProposal,
        *,
        generation: int,
    ) -> ProposalAcceptance:
        """FIFO-accept one proposal into the open generation or its successor."""
        if generation < 0:
            raise ValueError
        async with self._lock:
            accepted_generation = generation
            while accepted_generation in self._release_decisions:
                accepted_generation += 1
            self._acceptance_sequence += 1
            self._generation_proposals.setdefault(accepted_generation, []).append(proposal)
            return ProposalAcceptance(
                requested_generation=generation,
                accepted_generation=accepted_generation,
                acceptance_sequence=self._acceptance_sequence,
            )

    async def release(
        self,
        *,
        lease: AttemptLease,
        generation: int,
        started_monotonic: float,
    ) -> FrameReleaseDecision:
        """Atomically close one accepted batch and commit Continue or Stop."""
        async with self._lock:
            existing = self._release_decisions.get(generation)
            if existing is not None:
                return existing
            proposals = tuple(self._generation_proposals.pop(generation, ()))
            cutoff = self._acceptance_sequence
            if not proposals:
                release = FrameReleaseDecision(
                    generation=generation,
                    kind=FrameReleaseKind.CONTINUE,
                    acceptance_cutoff=cutoff,
                    terminal=None,
                )
            else:
                terminal = self._arbiter.register_batch(proposals)
                if terminal.cas_success:
                    await self._persist_winner_shielded(
                        lease,
                        terminal.winner,
                        started_monotonic,
                    )
                release = FrameReleaseDecision(
                    generation=generation,
                    kind=FrameReleaseKind.STOP,
                    acceptance_cutoff=cutoff,
                    terminal=terminal,
                )
            self._release_decisions[generation] = release
            return release

    async def has_pending(self, *, generation: int) -> bool:
        """Observe whether an open generation already has an accepted proposal."""
        async with self._lock:
            return bool(self._generation_proposals.get(generation))

    async def persisted_status_class(self) -> LastStatusClass:
        """Return the safe terminal status only after its durable commit completed."""
        async with self._lock:
            if self._persisted_status_class is None:
                raise RuntimeError
            return self._persisted_status_class

    async def finalize(
        self,
        *,
        lease: AttemptLease,
        proposals: tuple[TerminalProposal, ...],
        started_monotonic: float,
    ) -> TerminalDecision:
        """Latch and persist the winner before returning any terminal payload."""
        async with self._lock:
            decision = self._arbiter.register_batch(proposals)
            if not decision.cas_success:
                return decision
            await self._persist_winner_shielded(lease, decision.winner, started_monotonic)
            return decision

    async def _persist_winner_shielded(
        self,
        lease: AttemptLease,
        winner: TerminalProposal,
        started_monotonic: float,
    ) -> None:
        """Finish a latched winner's bounded durable commit under ancestor cancellation."""
        with anyio.CancelScope(shield=True):
            await self._persist_winner(lease, winner, started_monotonic)

    async def _persist_winner(
        self,
        lease: AttemptLease,
        winner: TerminalProposal,
        started_monotonic: float,
    ) -> None:
        latency_ms = max(
            0,
            int((self._dependencies.monotonic_clock.monotonic() - started_monotonic) * 1000),
        )
        command = AttemptFinalizeCommand(
            identity=lease.identity,
            outcome=winner.outcome,
            status_class=winner.status_class,
            latency_ms=latency_ms,
            cooldown_until=winner.cooldown_until,
            cooldown_kind=winner.cooldown_kind,
            terminal_committed_at=self._dependencies.clock.now(),
        )
        _ = await self._dependencies.attempts.finalize_attempt(command)
        self._persisted_status_class = command.status_class
