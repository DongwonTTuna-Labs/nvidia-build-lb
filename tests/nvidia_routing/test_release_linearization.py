"""Cancellation placement around the one frame-release linearization point."""

from datetime import UTC, datetime
from typing import override
from uuid import uuid4

import anyio
import pytest
from anyio.lowlevel import checkpoint
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
_BRANCHES = ("pre_commit", "committed_continue", "committed_stop")


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


class _BlockingAttempts(_Attempts):
    started: anyio.Event
    finish: anyio.Event
    completed: bool

    def __init__(self) -> None:
        super().__init__()
        self.started = anyio.Event()
        self.finish = anyio.Event()
        self.completed = False

    @override
    async def finalize_attempt(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        self.commands.append(command)
        self.started.set()
        await self.finish.wait()
        self.completed = True
        return TerminalCommitted(command.identity)


def _lease() -> AttemptLease:
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)
    return AttemptLease(identity, uuid4(), SecretStr("synthetic"))


@pytest.mark.parametrize("branch", _BRANCHES, ids=_BRANCHES)
async def test_release_linearization_branches(branch: str) -> None:
    attempts = _Attempts()
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    lease = _lease()
    anext_calls = 0

    if branch == "pre_commit":
        _ = await supervisor.coordinate(
            TerminalProposal.for_kind(TerminalKind.ANCESTOR_CANCELLED, sequence=1),
            generation=0,
        )
        release = await supervisor.release(lease=lease, generation=0, started_monotonic=1.0)
        expected_winner = TerminalKind.ANCESTOR_CANCELLED
        assert release.kind is FrameReleaseKind.STOP
    elif branch == "committed_continue":
        release = await supervisor.release(lease=lease, generation=0, started_monotonic=1.0)
        late = await supervisor.coordinate(
            TerminalProposal.for_kind(TerminalKind.ANCESTOR_CANCELLED, sequence=1),
            generation=0,
        )
        anext_calls += 1
        expected_winner = None
        assert release.kind is FrameReleaseKind.CONTINUE
        assert late.accepted_generation == 1
    else:
        _ = await supervisor.coordinate(
            TerminalProposal.for_kind(TerminalKind.DEADLINE_EXPIRED, sequence=1),
            generation=0,
        )
        release = await supervisor.release(lease=lease, generation=0, started_monotonic=1.0)
        late = await supervisor.coordinate(
            TerminalProposal.for_kind(TerminalKind.ANCESTOR_CANCELLED, sequence=2),
            generation=0,
        )
        expected_winner = TerminalKind.DEADLINE_EXPIRED
        assert release.kind is FrameReleaseKind.STOP
        assert late.accepted_generation == 1

    if expected_winner is None:
        assert release.terminal is None
        assert attempts.commands == []
        assert anext_calls == 1
    else:
        assert release.terminal is not None
        assert release.terminal.winner.kind is expected_winner
        assert len(attempts.commands) == 1
        assert anext_calls == 0


async def test_latched_terminal_persistence_survives_ancestor_cancellation() -> None:
    attempts = _BlockingAttempts()
    supervisor = ChatSupervisor(
        ChatSupervisorDependencies(attempts=attempts, clock=_Clock(), monotonic_clock=_Clock())
    )
    lease = _lease()
    releases: list[object] = []
    scopes: list[anyio.CancelScope] = []
    _ = await supervisor.coordinate(
        TerminalProposal.network_success(sequence=1, payload=b"data: [DONE]\n\n"),
        generation=0,
    )

    async def release_in_cancel_scope() -> None:
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            releases.append(
                await supervisor.release(
                    lease=lease,
                    generation=0,
                    started_monotonic=1.0,
                )
            )

    async with anyio.create_task_group() as tasks:
        _ = tasks.start_soon(release_in_cancel_scope)
        await attempts.started.wait()
        scopes[0].cancel()
        await checkpoint()
        attempts.finish.set()

    assert attempts.completed is True
    assert len(attempts.commands) == 1
    assert len(releases) == 1
