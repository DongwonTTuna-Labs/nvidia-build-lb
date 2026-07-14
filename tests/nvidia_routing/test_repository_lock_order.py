"""Approved scheduler-to-single-key lock order across every routing writer."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import override
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import (
    LastStatusClass,
    UpstreamKeyCreateRequest,
    UpstreamKeyRead,
)
from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.attempt_repository import (
    AttemptRepository,
    AttemptRepositoryDependencies,
)
from nvidia_build_lb.attempt_types import AttemptFinalizeCommand, AttemptStartCommand
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.scheduler_state import (
    AttemptTerminal,
    SchedulerDependencies,
    SchedulerStateRepository,
    TerminalOutcome,
)
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _FailStop(AttemptFailStop):
    @override
    def trigger(self) -> None:
        raise AssertionError


@asynccontextmanager
async def _capture_statements(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[list[str]]:
    async with sessions() as probe_session:
        bind = probe_session.bind
    assert isinstance(bind, AsyncEngine)
    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(bind.sync_engine, "before_cursor_execute", record_statement)
    try:
        yield statements
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", record_statement)


def _assert_scheduler_then_one_key(statements: list[str]) -> None:
    locking = tuple(
        statement
        for statement in statements
        if "FOR UPDATE" in statement
        and ("FROM scheduler_state" in statement or "FROM upstream_keys" in statement)
    )
    assert len(locking) == 2
    assert "FROM scheduler_state" in locking[0]
    assert "WHERE scheduler_state.singleton_id =" in locking[0]
    assert "FROM upstream_keys" in locking[1]
    assert "WHERE upstream_keys.id =" in locking[1]


async def _created_key(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    clock: Clock,
    credential: str,
) -> tuple[UpstreamKeyRepository, UpstreamKeyRead]:
    repository = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock))
    created = await repository.create(
        UpstreamKeyCreateRequest(key=credential),
        request_id=f"create-{credential}",
    )
    return repository, created


async def test_new_reservation_and_finalization_lock_scheduler_then_one_key(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    upstream, created = await _created_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "new-lock-order",
    )
    await upstream.enable(created.id, "new-lock-order-enable")
    repository = AttemptRepository(
        AttemptRepositoryDependencies(
            routing_session_factory,
            vault,
            fixed_clock,
            _FailStop(),
        )
    )
    command = AttemptStartCommand(
        started_event_id=uuid4(),
        terminal_event_id=uuid4(),
        request_id="new-lock-order",
        service_epoch=uuid4(),
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset(),
        started_at=fixed_clock.now(),
    )

    async with _capture_statements(routing_session_factory) as statements:
        lease = await repository.reserve_attempt(command)
    _assert_scheduler_then_one_key(statements)

    async with _capture_statements(routing_session_factory) as statements:
        repeated = await repository.reserve_attempt(command)
    _assert_scheduler_then_one_key(statements)
    assert repeated.key_id == lease.key_id

    terminal = AttemptFinalizeCommand.success(
        lease.identity,
        fixed_clock.now(),
        latency_ms=1,
    )
    async with _capture_statements(routing_session_factory) as statements:
        _ = await repository.finalize_attempt(terminal)
    _assert_scheduler_then_one_key(statements)

    async with _capture_statements(routing_session_factory) as statements:
        _ = await repository.finalize_attempt(terminal)
    _assert_scheduler_then_one_key(statements)


async def test_legacy_reservation_and_finalization_lock_scheduler_then_one_key(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    upstream, created = await _created_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "legacy-lock-order",
    )
    await upstream.enable(created.id, "legacy-lock-order-enable")
    repository = SchedulerStateRepository(
        SchedulerDependencies(routing_session_factory, fixed_clock)
    )

    async with _capture_statements(routing_session_factory) as statements:
        lease = await repository.begin_next_attempt("legacy-lock-order")
    _assert_scheduler_then_one_key(statements)

    terminal = AttemptTerminal(
        TerminalOutcome.SUCCEEDED,
        LastStatusClass.SUCCESS,
        1,
        None,
    )
    async with _capture_statements(routing_session_factory) as statements:
        await repository.finish_attempt(lease, terminal)
    _assert_scheduler_then_one_key(statements)


@pytest.mark.parametrize("operation", ["enable", "disable", "delete"])
async def test_admin_key_writer_locks_scheduler_then_one_key(
    operation: str,
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    repository, created = await _created_key(
        routing_session_factory,
        vault,
        fixed_clock,
        f"{operation}-lock-order",
    )
    if operation == "disable":
        await repository.enable(created.id, "disable-lock-order-setup")

    async with _capture_statements(routing_session_factory) as statements:
        if operation == "enable":
            await repository.enable(created.id, "enable-lock-order")
        elif operation == "disable":
            await repository.disable(created.id, "disable-lock-order")
        else:
            await repository.delete(created.id, "delete-lock-order")

    _assert_scheduler_then_one_key(statements)
