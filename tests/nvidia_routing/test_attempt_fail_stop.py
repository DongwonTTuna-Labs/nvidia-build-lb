"""Unresolved attempt commits withdraw readiness and cancel the process root."""

from typing import override
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.attempt_commit import (
    AttemptCommitUnresolvedError,
    finalize_with_reconciliation,
    reserve_with_reconciliation,
)
from nvidia_build_lb.attempt_fail_stop import LifecycleAttemptFailStop
from nvidia_build_lb.attempt_repository import (
    AttemptRepository,
    AttemptRepositoryDependencies,
)
from nvidia_build_lb.attempt_types import (
    AttemptLease,
    AttemptStartCommand,
    StartUnknown,
    TerminalCommitted,
    TerminalUnknown,
)
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _Readiness:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def set_ready(self, ready: bool) -> None:
        self.events.append(f"ready:{str(ready).lower()}")


class _RootCancellation:
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def cancel(self) -> None:
        self.events.append("root:cancel")


class _FailingReadiness(_Readiness):
    @override
    def set_ready(self, ready: bool) -> None:
        super().set_ready(ready)
        raise OSError


class _FailingRootCancellation(_RootCancellation):
    @override
    def cancel(self) -> None:
        super().cancel()
        raise OSError


def _start(clock: Clock) -> AttemptStartCommand:
    return AttemptStartCommand(
        started_event_id=uuid4(),
        terminal_event_id=uuid4(),
        request_id="fatal-start",
        service_epoch=uuid4(),
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset(),
        started_at=clock.now(),
    )


async def test_repository_unresolved_start_triggers_ordered_process_fail_stop(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(events),
        _RootCancellation(events),
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(
            routing_session_factory,
            vault,
            fixed_clock,
            fail_stop,
        )
    )

    async def ambiguous_start(
        _repository: AttemptRepository,
        _command: AttemptStartCommand,
    ) -> AttemptLease:
        raise SQLAlchemyError

    async def unknown_start(
        _repository: AttemptRepository,
        _command: AttemptStartCommand,
    ) -> StartUnknown:
        return StartUnknown()

    monkeypatch.setattr(AttemptRepository, "_reserve_once", ambiguous_start)
    monkeypatch.setattr(AttemptRepository, "reconcile_start", unknown_start)

    with pytest.raises(AttemptCommitUnresolvedError) as captured:
        _ = await repository.reserve_attempt(_start(fixed_clock))

    assert events == ["ready:false", "root:cancel"]
    assert fail_stop.triggered is True
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_fail_stop_transition_is_one_shot() -> None:
    events: list[str] = []
    fail_stop = LifecycleAttemptFailStop(
        _Readiness(events),
        _RootCancellation(events),
    )

    fail_stop.trigger()
    fail_stop.trigger()

    assert events == ["ready:false", "root:cancel"]


async def test_fail_stop_collaborator_errors_never_mask_original_unresolved(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    fail_stop = LifecycleAttemptFailStop(
        _FailingReadiness(events),
        _FailingRootCancellation(events),
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(
            routing_session_factory,
            vault,
            fixed_clock,
            fail_stop,
        )
    )

    async def ambiguous_start(
        _repository: AttemptRepository,
        _command: AttemptStartCommand,
    ) -> AttemptLease:
        raise SQLAlchemyError

    async def unknown_start(
        _repository: AttemptRepository,
        _command: AttemptStartCommand,
    ) -> StartUnknown:
        return StartUnknown()

    monkeypatch.setattr(AttemptRepository, "_reserve_once", ambiguous_start)
    monkeypatch.setattr(AttemptRepository, "reconcile_start", unknown_start)

    with pytest.raises(AttemptCommitUnresolvedError) as captured:
        _ = await repository.reserve_attempt(_start(fixed_clock))

    assert events == ["ready:false", "root:cancel"]
    assert fail_stop.collaborator_failure is True
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


async def test_reconciliation_database_errors_map_to_context_free_unresolved() -> None:
    async def ambiguous_start() -> AttemptLease:
        raise SQLAlchemyError

    async def failed_start_reconciliation() -> StartUnknown:
        raise SQLAlchemyError

    async def ambiguous_terminal() -> TerminalCommitted:
        raise SQLAlchemyError

    async def failed_terminal_reconciliation() -> TerminalUnknown:
        raise SQLAlchemyError

    with pytest.raises(AttemptCommitUnresolvedError) as start_error:
        _ = await reserve_with_reconciliation(
            ambiguous_start,
            failed_start_reconciliation,
        )
    with pytest.raises(AttemptCommitUnresolvedError) as terminal_error:
        _ = await finalize_with_reconciliation(
            ambiguous_terminal,
            failed_terminal_reconciliation,
        )

    for captured in (start_error.value, terminal_error.value):
        assert captured.__cause__ is None
        assert captured.__context__ is None
