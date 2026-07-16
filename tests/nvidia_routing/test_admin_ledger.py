"""Exact admission, reservation, retention, and blocker sensors."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.active_routed_requests import ActiveRoutedRequestRegistry
from nvidia_build_lb.admin.schemas import (
    CapacityBlocker,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    UpstreamKeyCreateRequest,
)
from nvidia_build_lb.admin_ledger import (
    AdminLedger,
    AdminLedgerPolicy,
    LedgerCapacityExhaustedError,
)
from nvidia_build_lb.admin_maintenance import AdminMaintenance
from nvidia_build_lb.attempt_repository import AttemptRepository, AttemptRepositoryDependencies
from nvidia_build_lb.attempt_types import AttemptFinalizeCommand, AttemptStartCommand
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.db_models import (
    EVENT_WRITER_GENERATION,
    AdminEventRow,
    AdminLedgerStateRow,
    UpstreamAttemptReceiptRow,
    UpstreamKeyRow,
    UpstreamLivePinRow,
)
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _FailStop:
    def trigger(self) -> None:
        """The happy-path admission sensor must not fail-stop."""


@dataclass(slots=True)
class _MutableClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


async def _enabled_key(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    clock: Clock,
    ledger: AdminLedger,
    credential: str = "ledger-capacity-key",
) -> tuple[UpstreamKeyRepository, UUID]:
    upstream = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock, ledger))
    created = await upstream.create(
        UpstreamKeyCreateRequest(key=credential),
        request_id=f"ledger-key-create-{credential}",
    )
    async with sessions.begin() as session:
        row = await session.get(UpstreamKeyRow, created.id, with_for_update=True)
        assert row is not None
        row.health_state = HealthState.HEALTHY.value
    await upstream.enable(created.id, request_id=f"ledger-key-enable-{credential}")
    return upstream, created.id


def _start(
    clock: Clock,
    *,
    request_id: str | None = None,
    excluded_key_ids: frozenset[UUID] | None = None,
) -> AttemptStartCommand:
    return AttemptStartCommand(
        started_event_id=uuid4(),
        terminal_event_id=uuid4(),
        request_id=uuid4().hex if request_id is None else request_id,
        service_epoch=uuid4(),
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset() if excluded_key_ids is None else excluded_key_ids,
        started_at=clock.now(),
    )


async def test_terminal_commit_consumes_reserved_slot_at_hard_cap(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    ledger = AdminLedger(
        routing_session_factory,
        fixed_clock,
        AdminLedgerPolicy(event_max_rows=4, attempt_max_rows=1),
    )
    upstream, key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        ledger,
    )
    attempts = AttemptRepository(
        AttemptRepositoryDependencies(
            routing_session_factory,
            vault,
            fixed_clock,
            _FailStop(),
            ledger,
        )
    )
    lease = await attempts.reserve_attempt(_start(fixed_clock))

    _ = await attempts.finalize_attempt(
        AttemptFinalizeCommand(
            identity=lease.identity,
            outcome=TerminalOutcome.SUCCEEDED,
            status_class=LastStatusClass.SUCCESS,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=fixed_clock.now() + timedelta(seconds=1),
        )
    )

    async with routing_session_factory() as session:
        event_rows = await session.scalar(select(func.count()).select_from(AdminEventRow))
        receipt = await session.get(UpstreamAttemptReceiptRow, lease.identity.started_event_id)
        key = await session.get(UpstreamKeyRow, key_id)
    assert event_rows == 4
    assert receipt is not None
    assert receipt.terminal_committed_at is not None
    assert key is not None
    assert key.success_count == 1

    with pytest.raises(LedgerCapacityExhaustedError):
        _ = await attempts.reserve_attempt(_start(fixed_clock))
    with pytest.raises(LedgerCapacityExhaustedError):
        await upstream.disable(key_id, request_id="blocked-disable")
    async with routing_session_factory() as session:
        key = await session.get(UpstreamKeyRow, key_id)
        event_rows = await session.scalar(select(func.count()).select_from(AdminEventRow))
    assert key is not None
    assert key.enabled is True
    assert event_rows == 4


def test_active_registry_releases_only_after_owner_scope() -> None:
    registry = ActiveRoutedRequestRegistry()
    request_id = uuid4().hex

    with registry.track(request_id):
        assert registry.contains(request_id)
        assert registry.snapshot() == frozenset({request_id})

    assert not registry.contains(request_id)


async def _complete_two_attempt_group(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    clock: Clock,
    ledger: AdminLedger,
    key_suffix: str = "",
) -> tuple[str, tuple[UUID, UUID]]:
    _, _ = await _enabled_key(sessions, vault, clock, ledger, f"retention-key-a{key_suffix}")
    _, _ = await _enabled_key(sessions, vault, clock, ledger, f"retention-key-b{key_suffix}")
    attempts = AttemptRepository(
        AttemptRepositoryDependencies(sessions, vault, clock, _FailStop(), ledger)
    )
    request_id = uuid4().hex
    first = await attempts.reserve_attempt(_start(clock, request_id=request_id))
    _ = await attempts.finalize_attempt(
        AttemptFinalizeCommand(
            identity=first.identity,
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.REQUEST_REJECTED,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=clock.now() + timedelta(seconds=1),
        )
    )
    second = await attempts.reserve_attempt(
        _start(
            clock,
            request_id=request_id,
            excluded_key_ids=frozenset({first.key_id}),
        )
    )
    _ = await attempts.finalize_attempt(
        AttemptFinalizeCommand(
            identity=second.identity,
            outcome=TerminalOutcome.SUCCEEDED,
            status_class=LastStatusClass.SUCCESS,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=clock.now() + timedelta(seconds=1),
        )
    )
    return request_id, (first.identity.started_event_id, second.identity.started_event_id)


async def _seed_newest_event_reserve(
    sessions: async_sessionmaker[AsyncSession],
    clock: Clock,
) -> None:
    async with sessions.begin() as session:
        for index in range(100):
            session.add(
                AdminEventRow(
                    id=uuid4(),
                    request_id=f"newest-reserve-{index}",
                    event_type=EventType.DOWNSTREAM_ISSUED.value,
                    upstream_key_id=None,
                    upstream_key_fingerprint=None,
                    downstream_token_id=None,
                    outcome_class=EventOutcome.SUCCEEDED.value,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=clock.now() + timedelta(seconds=index + 10),
                    attempt_started_event_id=None,
                    writer_generation=EVENT_WRITER_GENERATION,
                )
            )


async def test_minimum_prune_batch_deletes_one_complete_two_attempt_group(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    clock = _MutableClock(fixed_clock.now())
    ledger = AdminLedger(
        routing_session_factory,
        clock,
        AdminLedgerPolicy(event_max_rows=109, attempt_max_rows=3, prune_batch_size=6),
    )
    request_id, receipt_ids = await _complete_two_attempt_group(
        routing_session_factory,
        vault,
        clock,
        ledger,
    )
    await _seed_newest_event_reserve(routing_session_factory, clock)
    clock.advance(400)

    result = await AdminMaintenance(ledger).run_once()

    assert result.deleted_event_rows == 4
    assert result.deleted_attempt_rows == 2
    assert result.capacity_available is True
    assert result.capacity_blocker is CapacityBlocker.NONE
    async with routing_session_factory() as session:
        receipts = tuple(
            (
                await session.scalars(
                    select(UpstreamAttemptReceiptRow).where(
                        UpstreamAttemptReceiptRow.started_event_id.in_(receipt_ids)
                    )
                )
            ).all()
        )
        routed_events = tuple(
            (
                await session.scalars(
                    select(AdminEventRow).where(AdminEventRow.request_id == request_id)
                )
            ).all()
        )
        ledger_row = await session.get(AdminLedgerStateRow, 1)
    assert receipts == ()
    assert routed_events == ()
    assert ledger_row is not None
    assert ledger_row.rolled_up_routed_request_count == 1


async def test_one_pass_fills_row_budget_with_multiple_complete_attempt_groups(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    clock = _MutableClock(fixed_clock.now())
    ledger = AdminLedger(
        routing_session_factory,
        clock,
        AdminLedgerPolicy(event_max_rows=115, attempt_max_rows=5, prune_batch_size=12),
    )
    _, first_receipts = await _complete_two_attempt_group(
        routing_session_factory,
        vault,
        clock,
        ledger,
    )
    _, second_receipts = await _complete_two_attempt_group(
        routing_session_factory,
        vault,
        clock,
        ledger,
        "-second",
    )
    await _seed_newest_event_reserve(routing_session_factory, clock)
    clock.advance(400)

    result = await AdminMaintenance(ledger).run_once()

    assert result.deleted_event_rows == 8
    assert result.deleted_attempt_rows == 4
    assert result.capacity_available is True
    async with routing_session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(UpstreamAttemptReceiptRow)
            .where(
                UpstreamAttemptReceiptRow.started_event_id.in_((*first_receipts, *second_receipts))
            )
        )
        ledger_row = await session.get(AdminLedgerStateRow, 1)
    assert remaining == 0
    assert ledger_row is not None
    assert ledger_row.rolled_up_routed_request_count == 2


async def test_active_routed_request_survives_grace_clock_jump_without_prune(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    clock = _MutableClock(fixed_clock.now())
    registry = ActiveRoutedRequestRegistry()
    ledger = AdminLedger(
        routing_session_factory,
        clock,
        AdminLedgerPolicy(event_max_rows=105, attempt_max_rows=3, prune_batch_size=6),
        registry,
    )
    request_id, receipt_ids = await _complete_two_attempt_group(
        routing_session_factory,
        vault,
        clock,
        ledger,
    )
    await _seed_newest_event_reserve(routing_session_factory, clock)
    clock.advance(10_000)
    registry.register(request_id)
    try:
        result = await AdminMaintenance(ledger).run_once()
    finally:
        registry.release(request_id)

    assert result.deleted_event_rows == 4
    assert result.deleted_attempt_rows == 0
    assert result.capacity_available is False
    assert result.capacity_blocker is CapacityBlocker.ACTIVE_ATTEMPTS
    async with routing_session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(UpstreamAttemptReceiptRow)
            .where(UpstreamAttemptReceiptRow.started_event_id.in_(receipt_ids))
        )
        routed_event_count = await session.scalar(
            select(func.count())
            .select_from(AdminEventRow)
            .where(AdminEventRow.request_id == request_id)
        )
    assert remaining == 2
    assert routed_event_count == 4


async def test_settlement_grace_prevents_failover_request_double_count(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    clock = _MutableClock(fixed_clock.now())
    ledger = AdminLedger(
        routing_session_factory,
        clock,
        AdminLedgerPolicy(event_max_rows=105, attempt_max_rows=3, prune_batch_size=6),
    )
    request_id, receipt_ids = await _complete_two_attempt_group(
        routing_session_factory,
        vault,
        clock,
        ledger,
    )
    await _seed_newest_event_reserve(routing_session_factory, clock)
    clock.advance(299)

    result = await AdminMaintenance(ledger).run_once()

    assert result.deleted_event_rows == 4
    assert result.deleted_attempt_rows == 0
    assert result.capacity_available is False
    assert result.capacity_blocker is CapacityBlocker.RECONCILIATION_GRACE
    async with routing_session_factory() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(UpstreamAttemptReceiptRow)
            .where(UpstreamAttemptReceiptRow.started_event_id.in_(receipt_ids))
        )
        ledger_row = await session.get(AdminLedgerStateRow, 1)
        routed_event_count = await session.scalar(
            select(func.count())
            .select_from(AdminEventRow)
            .where(AdminEventRow.request_id == request_id)
        )
    assert remaining == 2
    assert routed_event_count == 4
    assert ledger_row is not None
    assert ledger_row.rolled_up_routed_request_count == 0


async def test_partial_skip_locked_request_group_is_never_pruned_or_rolled_up(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    clock = _MutableClock(fixed_clock.now())
    ledger = AdminLedger(
        routing_session_factory,
        clock,
        AdminLedgerPolicy(event_max_rows=105, attempt_max_rows=3, prune_batch_size=6),
    )
    request_id, receipt_ids = await _complete_two_attempt_group(
        routing_session_factory,
        vault,
        clock,
        ledger,
    )
    await _seed_newest_event_reserve(routing_session_factory, clock)
    clock.advance(400)

    async with routing_session_factory.begin() as locking_session:
        locked = await locking_session.get(
            UpstreamAttemptReceiptRow,
            receipt_ids[0],
            with_for_update=True,
        )
        assert locked is not None
        result = await AdminMaintenance(ledger).run_once()

    assert result.deleted_event_rows == 0
    assert result.deleted_attempt_rows == 0
    assert result.capacity_available is False
    assert result.capacity_blocker is CapacityBlocker.LOCK_CONTENTION
    async with routing_session_factory() as session:
        receipt_count = await session.scalar(
            select(func.count())
            .select_from(UpstreamAttemptReceiptRow)
            .where(UpstreamAttemptReceiptRow.started_event_id.in_(receipt_ids))
        )
        event_count = await session.scalar(
            select(func.count())
            .select_from(AdminEventRow)
            .where(AdminEventRow.request_id == request_id)
        )
        ledger_row = await session.get(AdminLedgerStateRow, 1)
    assert receipt_count == 2
    assert event_count == 4
    assert ledger_row is not None
    assert ledger_row.rolled_up_routed_request_count == 0


async def test_orphaned_pending_receipt_reports_capacity_blocked(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    ledger = AdminLedger(
        routing_session_factory,
        fixed_clock,
        AdminLedgerPolicy(event_max_rows=4, attempt_max_rows=1),
    )
    _, _ = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        ledger,
        "orphaned-pending-key",
    )
    attempts = AttemptRepository(
        AttemptRepositoryDependencies(
            routing_session_factory,
            vault,
            fixed_clock,
            _FailStop(),
            ledger,
        )
    )
    lease = await attempts.reserve_attempt(_start(fixed_clock))
    async with routing_session_factory.begin() as session:
        pin = await session.get(UpstreamLivePinRow, lease.identity.started_event_id)
        assert pin is not None
        await session.delete(pin)

    result = await AdminMaintenance(ledger).run_once()

    assert result.deleted_attempt_rows == 0
    assert result.capacity_available is False
    assert result.capacity_blocker is CapacityBlocker.ORPHANED_PENDING
    async with routing_session_factory() as session:
        receipt = await session.get(
            UpstreamAttemptReceiptRow,
            lease.identity.started_event_id,
        )
        ledger_row = await session.get(AdminLedgerStateRow, 1)
    assert receipt is not None
    assert receipt.terminal_committed_at is None
    assert ledger_row is not None
    assert ledger_row.last_capacity_blocker == CapacityBlocker.ORPHANED_PENDING.value

    expanded = AdminLedger(
        routing_session_factory,
        fixed_clock,
        AdminLedgerPolicy(event_max_rows=100, attempt_max_rows=10),
    )
    expanded_result = await AdminMaintenance(expanded).run_once()
    assert expanded_result.capacity_available is False
    assert expanded_result.capacity_blocker is CapacityBlocker.ORPHANED_PENDING
    async with routing_session_factory.begin() as session:
        with pytest.raises(LedgerCapacityExhaustedError):
            _ = await expanded.lock_attempt_admission(session)


async def test_legacy_unlinked_attempt_group_blocks_instead_of_pruning(
    routing_session_factory: async_sessionmaker[AsyncSession],
    fixed_clock: Clock,
) -> None:
    ledger = AdminLedger(
        routing_session_factory,
        fixed_clock,
        AdminLedgerPolicy(event_max_rows=2, attempt_max_rows=1),
    )
    legacy_event_id = uuid4()
    async with routing_session_factory.begin() as session:
        session.add(
            AdminEventRow(
                id=legacy_event_id,
                request_id="legacy-unlinked-request",
                event_type=EventType.UPSTREAM_ATTEMPT.value,
                upstream_key_id=None,
                upstream_key_fingerprint=None,
                downstream_token_id=None,
                outcome_class=EventOutcome.STARTED.value,
                status_class=None,
                latency_ms=None,
                occurred_at=fixed_clock.now(),
                attempt_started_event_id=None,
                writer_generation=EVENT_WRITER_GENERATION,
            )
        )

    result = await AdminMaintenance(ledger).run_once()

    assert result.deleted_event_rows == 0
    assert result.deleted_attempt_rows == 0
    assert result.capacity_available is False
    assert result.capacity_blocker is CapacityBlocker.LEGACY_UNLINKED
    async with routing_session_factory() as session:
        event = await session.get(AdminEventRow, legacy_event_id)
        ledger_row = await session.get(AdminLedgerStateRow, 1)
    assert event is not None
    assert ledger_row is not None
    assert ledger_row.last_capacity_blocker == CapacityBlocker.LEGACY_UNLINKED.value

    expanded = AdminLedger(
        routing_session_factory,
        fixed_clock,
        AdminLedgerPolicy(event_max_rows=100, attempt_max_rows=10),
    )
    expanded_result = await AdminMaintenance(expanded).run_once()
    assert expanded_result.capacity_available is False
    assert expanded_result.capacity_blocker is CapacityBlocker.LEGACY_UNLINKED
    async with routing_session_factory.begin() as session:
        with pytest.raises(LedgerCapacityExhaustedError):
            _ = await expanded.lock_attempt_admission(session)
