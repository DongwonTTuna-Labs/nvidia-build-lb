from datetime import timedelta
from uuid import UUID, uuid4

import anyio
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.admin.schemas import LastStatusClass, UpstreamKeyCreateRequest
from nvidia_build_lb.attempt_commit import (
    AttemptCommitUnresolvedError,
    StartConflictError,
    TerminalConflictError,
    reserve_with_reconciliation,
)
from nvidia_build_lb.attempt_records import pending_start_matches
from nvidia_build_lb.attempt_repository import (
    AttemptRepository,
    AttemptRepositoryDependencies,
)
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptIdentity,
    AttemptLease,
    AttemptStartCommand,
    CooldownKind,
    StartAbsent,
    StartCommitted,
    StartConflict,
    StartUnknown,
    TerminalAbsent,
    TerminalConflict,
    TerminalExact,
    TerminalPending,
)
from nvidia_build_lb.credential_types import Clock, ResourceConflictError
from nvidia_build_lb.db_models import (
    AdminEventRow,
    SchedulerStateRow,
    UpstreamAttemptReceiptRow,
    UpstreamKeyRow,
    UpstreamLivePinRow,
)
from nvidia_build_lb.scheduler_state import NoEligibleUpstreamKeyError, TerminalOutcome
from nvidia_build_lb.scheduler_types import (
    NoEligibleReason,
    SchedulerStateUnavailableError,
)
from nvidia_build_lb.service_epoch_connection import PsycopgEpochConnectionFactory
from nvidia_build_lb.upstream_keys import UpstreamKeyDependencies, UpstreamKeyRepository
from nvidia_build_lb.vault import Vault

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _FailStop:
    calls: int

    def __init__(self) -> None:
        self.calls = 0

    def trigger(self) -> None:
        self.calls += 1


async def _enabled_key(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    clock: Clock,
    credential: str,
) -> UUID:
    upstream = UpstreamKeyRepository(UpstreamKeyDependencies(sessions, vault, clock))
    created = await upstream.create(
        UpstreamKeyCreateRequest(key=credential),
        request_id=f"create-{credential}",
    )
    await upstream.enable(created.id, request_id=f"enable-{credential}")
    return created.id


def _start(clock: Clock, *, request_id: str = "attempt-1") -> AttemptStartCommand:
    return AttemptStartCommand(
        started_event_id=uuid4(),
        terminal_event_id=uuid4(),
        request_id=request_id,
        service_epoch=uuid4(),
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset(),
        started_at=clock.now(),
    )


def _probe_start(clock: Clock, key_id: UUID, request_id: str) -> AttemptStartCommand:
    command = _start(clock, request_id=request_id)
    return AttemptStartCommand(
        started_event_id=command.started_event_id,
        terminal_event_id=command.terminal_event_id,
        request_id=command.request_id,
        service_epoch=command.service_epoch,
        explicit_probe_key_id=key_id,
        excluded_key_ids=frozenset(),
        started_at=command.started_at,
    )


async def test_real_repository_no_eligible_error_survives_commit_budget(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )

    with pytest.raises(NoEligibleUpstreamKeyError, match="no_eligible_upstream_key"):
        _ = await repository.reserve_attempt(_start(fixed_clock, request_id="no-eligible"))


async def test_reserve_exact_retry_returns_same_lease_without_counter_delta(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-a",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    command = _start(fixed_clock)

    first = await repository.reserve_attempt(command)
    repeated = await repository.reserve_attempt(command)

    assert first.identity == repeated.identity
    assert first.key_id == repeated.key_id == key_id
    assert first.credential.get_secret_value() == "attempt-secret-a"
    async with routing_session_factory() as session:
        key = await session.get(UpstreamKeyRow, key_id)
        receipts = await session.scalar(select(func.count()).select_from(UpstreamAttemptReceiptRow))
        pins = await session.scalar(select(func.count()).select_from(UpstreamLivePinRow))
    assert key is not None
    assert key.request_count == 1
    assert receipts == pins == 1


async def test_reserve_same_ids_with_changed_request_is_conflict(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    _ = await _enabled_key(routing_session_factory, vault, fixed_clock, "attempt-secret-b")
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    command = _start(fixed_clock)
    _ = await repository.reserve_attempt(command)
    changed = AttemptStartCommand(
        started_event_id=command.started_event_id,
        terminal_event_id=command.terminal_event_id,
        request_id="changed-request",
        service_epoch=command.service_epoch,
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset(),
        started_at=command.started_at,
    )

    with pytest.raises(StartConflictError):
        _ = await repository.reserve_attempt(changed)


async def test_public_reservation_excludes_one_failed_key_and_selects_other(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    first = await _enabled_key(routing_session_factory, vault, fixed_clock, "attempt-secret-c")
    second = await _enabled_key(routing_session_factory, vault, fixed_clock, "attempt-secret-d")
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    command = _start(fixed_clock)
    excluded = AttemptStartCommand(
        started_event_id=command.started_event_id,
        terminal_event_id=command.terminal_event_id,
        request_id=command.request_id,
        service_epoch=command.service_epoch,
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset({first}),
        started_at=command.started_at,
    )

    lease = await repository.reserve_attempt(excluded)

    assert lease.key_id == second


async def test_failover_preserves_three_key_cursor_position_without_starvation(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    created = tuple(
        [
            await _enabled_key(
                routing_session_factory,
                vault,
                fixed_clock,
                f"attempt-secret-ring-{index}",
            )
            for index in range(3)
        ]
    )
    ordered = tuple(sorted(created, key=lambda value: value.bytes))
    async with routing_session_factory.begin() as session:
        scheduler = await session.get(SchedulerStateRow, 1, with_for_update=True)
        assert scheduler is not None
        scheduler.cursor_key_id = ordered[0]
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    command = _start(fixed_clock, request_id="three-key-failover")
    excluded = AttemptStartCommand(
        started_event_id=command.started_event_id,
        terminal_event_id=command.terminal_event_id,
        request_id=command.request_id,
        service_epoch=command.service_epoch,
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset({ordered[1]}),
        started_at=command.started_at,
    )

    lease = await repository.reserve_attempt(excluded)

    assert lease.key_id == ordered[2]


async def test_quarantined_cursor_preserves_three_key_ring_successor(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    created = tuple(
        [
            await _enabled_key(
                routing_session_factory,
                vault,
                fixed_clock,
                f"attempt-secret-quarantine-ring-{index}",
            )
            for index in range(3)
        ]
    )
    ordered = tuple(sorted(created, key=lambda value: value.bytes))
    async with routing_session_factory.begin() as session:
        scheduler = await session.get(SchedulerStateRow, 1, with_for_update=True)
        assert scheduler is not None
        scheduler.cursor_key_id = ordered[0]
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    failed = await repository.reserve_attempt(
        _start(fixed_clock, request_id="quarantine-ring-failed")
    )
    assert failed.key_id == ordered[1]
    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand(
            identity=failed.identity,
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.INVALID_CREDENTIAL,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=fixed_clock.now(),
        )
    )
    alternate = _start(fixed_clock, request_id="quarantine-ring-alternate")
    alternate = AttemptStartCommand(
        started_event_id=alternate.started_event_id,
        terminal_event_id=alternate.terminal_event_id,
        request_id=alternate.request_id,
        service_epoch=alternate.service_epoch,
        explicit_probe_key_id=None,
        excluded_key_ids=frozenset({failed.key_id}),
        started_at=alternate.started_at,
    )

    lease = await repository.reserve_attempt(alternate)

    assert lease.key_id == ordered[2]


async def test_two_key_scheduler_strictly_alternates_repeated_reservations(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    created = tuple(
        [
            await _enabled_key(
                routing_session_factory,
                vault,
                fixed_clock,
                f"attempt-secret-alternate-{index}",
            )
            for index in range(2)
        ]
    )
    ordered = tuple(sorted(created, key=lambda value: value.bytes))
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )

    observed = tuple(
        [
            (
                await repository.reserve_attempt(
                    _start(fixed_clock, request_id=f"strict-alternate-{index}")
                )
            ).key_id
            for index in range(6)
        ]
    )

    assert observed == ordered * 3


async def test_concurrent_reservations_remain_evenly_distributed(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    keys = tuple(
        [
            await _enabled_key(
                routing_session_factory,
                vault,
                fixed_clock,
                f"attempt-secret-concurrent-{index}",
            )
            for index in range(2)
        ]
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    observed: list[UUID] = []

    async def reserve(index: int) -> None:
        lease = await repository.reserve_attempt(
            _start(fixed_clock, request_id=f"concurrent-reservation-{index}")
        )
        observed.append(lease.key_id)

    async with anyio.create_task_group() as tasks:
        for index in range(20):
            _ = tasks.start_soon(reserve, index)

    assert len(observed) == 20
    assert {key_id: observed.count(key_id) for key_id in keys} == {
        keys[0]: 10,
        keys[1]: 10,
    }


async def test_successful_explicit_probe_recovers_quarantined_key(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-probe-recovery",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    failed_probe = await repository.reserve_attempt(
        _probe_start(fixed_clock, key_id, "failed-probe")
    )
    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand(
            identity=failed_probe.identity,
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.INVALID_CREDENTIAL,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=fixed_clock.now(),
        )
    )
    with pytest.raises(NoEligibleUpstreamKeyError):
        _ = await repository.reserve_attempt(_start(fixed_clock, request_id="quarantined-public"))

    recovered_probe = await repository.reserve_attempt(
        _probe_start(fixed_clock, key_id, "successful-probe")
    )
    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand.success(
            recovered_probe.identity,
            fixed_clock.now(),
            latency_ms=1,
        )
    )

    async with routing_session_factory() as session:
        key = await session.get(UpstreamKeyRow, key_id)
    assert key is not None
    assert key.quarantined is False
    assert key.health_state == "healthy"
    assert key.cooldown_until is None


async def test_late_public_success_cannot_recover_concurrently_quarantined_key(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-late-public-success",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    invalid_lease = await repository.reserve_attempt(
        _start(fixed_clock, request_id="public-invalid")
    )
    late_success_lease = await repository.reserve_attempt(
        _start(fixed_clock, request_id="public-late-success")
    )
    assert invalid_lease.key_id == late_success_lease.key_id == key_id

    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand(
            identity=invalid_lease.identity,
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.INVALID_CREDENTIAL,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=fixed_clock.now(),
        )
    )
    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand.success(
            late_success_lease.identity,
            fixed_clock.now(),
            latency_ms=1,
        )
    )

    async with routing_session_factory() as session:
        key = await session.get(UpstreamKeyRow, key_id)
    assert key is not None
    assert key.quarantined is True
    assert key.health_state == "degraded"
    assert key.last_status_class == LastStatusClass.INVALID_CREDENTIAL.value

    recovered_probe = await repository.reserve_attempt(
        _probe_start(fixed_clock, key_id, "explicit-recovery-after-race")
    )
    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand.success(
            recovered_probe.identity,
            fixed_clock.now(),
            latency_ms=1,
        )
    )

    async with routing_session_factory() as session:
        recovered = await session.get(UpstreamKeyRow, key_id)
    assert recovered is not None
    assert recovered.quarantined is False
    assert recovered.health_state == "healthy"
    assert recovered.last_status_class == LastStatusClass.SUCCESS.value


async def test_finalize_is_atomic_and_exact_repeat_has_zero_delta(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(routing_session_factory, vault, fixed_clock, "attempt-secret-e")
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    lease = await repository.reserve_attempt(_start(fixed_clock))
    terminal = AttemptFinalizeCommand(
        identity=lease.identity,
        outcome=TerminalOutcome.FAILED,
        status_class=LastStatusClass.RATE_LIMITED,
        latency_ms=19,
        cooldown_until=fixed_clock.now() + timedelta(seconds=45),
        cooldown_kind=CooldownKind.RATE_LIMIT,
        terminal_committed_at=fixed_clock.now(),
    )

    first = await repository.finalize_attempt(terminal)
    repeated = await repository.finalize_attempt(terminal)

    assert first == repeated
    async with routing_session_factory() as session:
        key = await session.get(UpstreamKeyRow, key_id)
        receipt = await session.get(UpstreamAttemptReceiptRow, lease.identity.started_event_id)
        pin = await session.get(UpstreamLivePinRow, lease.identity.started_event_id)
        terminal_event = await session.get(AdminEventRow, lease.identity.terminal_event_id)
    assert key is not None
    assert key.failure_count == 1
    assert key.cooldown_kind == CooldownKind.RATE_LIMIT.value
    assert receipt is not None
    assert receipt.terminal_status_class == "rate_limited"
    assert pin is None
    assert terminal_event is not None
    assert terminal_event.attempt_started_event_id == lease.identity.started_event_id


@pytest.mark.parametrize("corruption", ["missing_event", "stale_pin"])
async def test_finalize_exact_repeat_rejects_incomplete_completed_state(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
    corruption: str,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        f"attempt-secret-completed-{corruption}",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    start = _start(fixed_clock, request_id=f"completed-{corruption}")
    lease = await repository.reserve_attempt(start)
    terminal = AttemptFinalizeCommand.success(
        lease.identity,
        fixed_clock.now(),
        latency_ms=7,
    )
    _ = await repository.finalize_attempt(terminal)
    async with routing_session_factory.begin() as session:
        if corruption == "missing_event":
            event = await session.get(AdminEventRow, lease.identity.terminal_event_id)
            assert event is not None
            await session.delete(event)
        else:
            session.add(
                UpstreamLivePinRow(
                    started_event_id=lease.identity.started_event_id,
                    upstream_key_id=key_id,
                    service_epoch=lease.identity.service_epoch,
                    pinned_at=lease.identity.started_at,
                )
            )

    with pytest.raises(TerminalConflictError):
        _ = await repository.finalize_attempt(terminal)


async def test_finalize_same_identity_with_changed_latency_is_conflict(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    _ = await _enabled_key(routing_session_factory, vault, fixed_clock, "attempt-secret-f")
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    lease = await repository.reserve_attempt(_start(fixed_clock))
    terminal = AttemptFinalizeCommand.success(lease.identity, fixed_clock.now(), latency_ms=1)
    _ = await repository.finalize_attempt(terminal)

    with pytest.raises(TerminalConflictError):
        _ = await repository.finalize_attempt(
            AttemptFinalizeCommand.success(lease.identity, fixed_clock.now(), latency_ms=2)
        )


async def test_protocol_failure_degrades_health_without_quarantine_or_cooldown(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-protocol",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    lease = await repository.reserve_attempt(_start(fixed_clock, request_id="protocol"))

    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand(
            identity=lease.identity,
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=fixed_clock.now(),
        )
    )

    async with routing_session_factory() as session:
        key = await session.get(UpstreamKeyRow, key_id)
    assert key is not None
    assert key.health_state == "degraded"
    assert key.quarantined is False
    assert key.cooldown_until is None


async def test_quarantine_preserves_existing_cooldown_and_streaks(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-quarantine",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    lease = await repository.reserve_attempt(_start(fixed_clock, request_id="quarantine"))
    cooldown_until = fixed_clock.now() + timedelta(seconds=30)
    async with routing_session_factory.begin() as session:
        key = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
        assert key is not None
        key.cooldown_until = cooldown_until
        key.cooldown_kind = CooldownKind.TRANSIENT.value
        key.consecutive_rate_limits = 2
        key.consecutive_transient_failures = 3

    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand(
            identity=lease.identity,
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.INVALID_CREDENTIAL,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=fixed_clock.now(),
        )
    )

    async with routing_session_factory() as session:
        key = await session.get(UpstreamKeyRow, key_id)
    assert key is not None
    assert key.quarantined is True
    assert key.cooldown_until == cooldown_until
    assert key.cooldown_kind == CooldownKind.TRANSIENT.value
    assert key.consecutive_rate_limits == 2
    assert key.consecutive_transient_failures == 3


async def test_cleanup_prior_epoch_pins_never_removes_current_epoch(
    routing_session_factory: async_sessionmaker[AsyncSession],
    empty_database: SecretStr,
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    _ = await _enabled_key(routing_session_factory, vault, fixed_clock, "attempt-secret-g")
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    prior = await repository.reserve_attempt(_start(fixed_clock, request_id="prior"))
    current_command = _start(fixed_clock, request_id="current")
    current = await repository.reserve_attempt(current_command)

    connection = await PsycopgEpochConnectionFactory(empty_database).open()
    try:
        assert await connection.try_advisory_lock(1_312_967_746, 1) is True
        cleanup = await repository.cleanup_prior_epoch_pins(
            connection,
            current.identity.service_epoch,
        )
        assert await connection.advisory_unlock(1_312_967_746, 1) is True
    finally:
        await connection.close()

    assert cleanup.deleted_count == 1
    async with routing_session_factory() as session:
        prior_pin = await session.get(UpstreamLivePinRow, prior.identity.started_event_id)
        current_pin = await session.get(UpstreamLivePinRow, current.identity.started_event_id)
    assert prior_pin is None
    assert current_pin is not None


async def test_disabled_key_delete_is_blocked_only_while_live_pin_exists(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    upstream = UpstreamKeyRepository(
        UpstreamKeyDependencies(routing_session_factory, vault, fixed_clock)
    )
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-h",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    lease = await repository.reserve_attempt(_start(fixed_clock))
    await upstream.disable(key_id, request_id="disable-pinned")

    with pytest.raises(ResourceConflictError):
        await upstream.delete(key_id, request_id="delete-pinned")

    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand.success(lease.identity, fixed_clock.now(), latency_ms=1)
    )
    await upstream.delete(key_id, request_id="delete-after-finalize")


async def test_start_reconciliation_distinguishes_absent_exact_and_conflict(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    _ = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-reconcile-start",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    command = _start(fixed_clock, request_id="reconcile-start")

    assert isinstance(await repository.reconcile_start(command), StartAbsent)
    lease = await repository.reserve_attempt(command)
    exact = await repository.reconcile_start(command)
    assert isinstance(exact, StartCommitted)
    assert exact.lease.identity == lease.identity
    changed = AttemptStartCommand(
        started_event_id=command.started_event_id,
        terminal_event_id=command.terminal_event_id,
        request_id="different-request",
        service_epoch=command.service_epoch,
        explicit_probe_key_id=command.explicit_probe_key_id,
        excluded_key_ids=command.excluded_key_ids,
        started_at=command.started_at,
    )
    assert isinstance(await repository.reconcile_start(changed), StartConflict)


async def test_terminal_reconciliation_distinguishes_absent_pending_exact_and_conflict(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    _ = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "attempt-secret-reconcile-terminal",
    )
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    absent_identity = AttemptIdentity(
        uuid4(),
        uuid4(),
        "absent-terminal",
        uuid4(),
        fixed_clock.now(),
    )
    absent = AttemptFinalizeCommand.success(absent_identity, fixed_clock.now(), latency_ms=1)
    assert isinstance(await repository.reconcile_terminal(absent), TerminalAbsent)

    lease = await repository.reserve_attempt(_start(fixed_clock, request_id="reconcile-terminal"))
    terminal = AttemptFinalizeCommand.success(lease.identity, fixed_clock.now(), latency_ms=7)
    assert isinstance(await repository.reconcile_terminal(terminal), TerminalPending)
    _ = await repository.finalize_attempt(terminal)
    assert isinstance(await repository.reconcile_terminal(terminal), TerminalExact)
    changed = AttemptFinalizeCommand.success(lease.identity, fixed_clock.now(), latency_ms=8)
    assert isinstance(await repository.reconcile_terminal(changed), TerminalConflict)


async def test_commit_unknown_uses_fresh_reconciliation_without_blind_retry(
    fixed_clock: Clock,
) -> None:
    identity = AttemptIdentity(uuid4(), uuid4(), "commit-unknown", uuid4(), fixed_clock.now())
    lease = AttemptLease(identity, uuid4(), SecretStr("synthetic-secret"))
    calls: list[str] = []

    async def ambiguous_operation() -> AttemptLease:
        calls.append("operation")
        raise SQLAlchemyError

    async def fresh_reconciliation() -> StartCommitted:
        calls.append("reconcile")
        return StartCommitted(lease)

    resolved = await reserve_with_reconciliation(ambiguous_operation, fresh_reconciliation)

    assert resolved is lease
    assert calls == ["operation", "reconcile"]


async def test_unresolved_commit_unknown_fails_closed_without_operation_replay() -> None:
    calls: list[str] = []

    async def ambiguous_operation() -> AttemptLease:
        calls.append("operation")
        raise SQLAlchemyError

    async def unknown_reconciliation() -> StartUnknown:
        calls.append("reconcile")
        return StartUnknown()

    with pytest.raises(AttemptCommitUnresolvedError, match="attempt_commit_unresolved"):
        _ = await reserve_with_reconciliation(ambiguous_operation, unknown_reconciliation)

    assert calls == ["operation", "reconcile"]


async def test_scheduler_singleton_missing_is_database_unavailable_not_no_keys(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    async with routing_session_factory.begin() as session:
        scheduler = await session.get(SchedulerStateRow, 1, with_for_update=True)
        assert scheduler is not None
        await session.delete(scheduler)
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )

    with pytest.raises(SchedulerStateUnavailableError, match="scheduler_state_unavailable"):
        _ = await repository.reserve_attempt(_start(fixed_clock, request_id="missing-scheduler"))


async def test_locked_no_eligible_snapshot_uses_earliest_mixed_cooldown(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    first = await _enabled_key(routing_session_factory, vault, fixed_clock, "cooldown-rate-a")
    second = await _enabled_key(routing_session_factory, vault, fixed_clock, "cooldown-rate-b")
    transient = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "cooldown-transient",
    )
    async with routing_session_factory.begin() as session:
        for key_id, seconds, kind in (
            (first, 30, CooldownKind.RATE_LIMIT),
            (second, 10, CooldownKind.RATE_LIMIT),
            (transient, 5, CooldownKind.TRANSIENT),
        ):
            key = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
            assert key is not None
            key.cooldown_until = fixed_clock.now() + timedelta(seconds=seconds)
            key.cooldown_kind = kind.value
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )

    with pytest.raises(NoEligibleUpstreamKeyError) as captured:
        _ = await repository.reserve_attempt(_start(fixed_clock, request_id="cooldown-rate"))

    assert captured.value.reason is NoEligibleReason.RATE_COOLDOWN
    assert captured.value.retry_after_seconds == 5


async def test_locked_no_eligible_snapshot_classifies_transient_cooldown(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "cooldown-transient-only",
    )
    async with routing_session_factory.begin() as session:
        key = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
        assert key is not None
        key.cooldown_until = fixed_clock.now() + timedelta(seconds=4)
        key.cooldown_kind = CooldownKind.TRANSIENT.value
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )

    with pytest.raises(NoEligibleUpstreamKeyError) as captured:
        _ = await repository.reserve_attempt(_start(fixed_clock, request_id="cooldown-transient"))

    assert captured.value.reason is NoEligibleReason.TRANSIENT_COOLDOWN
    assert captured.value.retry_after_seconds is None


async def test_streak_snapshot_survives_exact_retry_and_fresh_reconciliation(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    key_id = await _enabled_key(
        routing_session_factory,
        vault,
        fixed_clock,
        "streak-snapshot",
    )
    async with routing_session_factory.begin() as session:
        key = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
        assert key is not None
        key.consecutive_rate_limits = 2
        key.consecutive_transient_failures = 3
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    command = _start(fixed_clock, request_id="streak-snapshot")

    first = await repository.reserve_attempt(command)
    async with routing_session_factory.begin() as session:
        key = await session.get(UpstreamKeyRow, key_id, with_for_update=True)
        assert key is not None
        key.consecutive_rate_limits = 8
        key.consecutive_transient_failures = 9
    repeated = await repository.reserve_attempt(command)
    reconciled = await repository.reconcile_start(command)

    assert first.consecutive_rate_limits == repeated.consecutive_rate_limits == 2
    assert first.consecutive_transient_failures == repeated.consecutive_transient_failures == 3
    assert isinstance(reconciled, StartCommitted)
    assert reconciled.lease.consecutive_rate_limits == 2
    assert reconciled.lease.consecutive_transient_failures == 3


async def test_completed_or_partial_start_receipt_never_reissues_a_lease(
    routing_session_factory: async_sessionmaker[AsyncSession],
    vault: Vault,
    fixed_clock: Clock,
) -> None:
    _ = await _enabled_key(routing_session_factory, vault, fixed_clock, "no-reissue")
    repository = AttemptRepository(
        AttemptRepositoryDependencies(routing_session_factory, vault, fixed_clock, _FailStop())
    )
    completed_command = _start(fixed_clock, request_id="completed-no-reissue")
    completed = await repository.reserve_attempt(completed_command)
    _ = await repository.finalize_attempt(
        AttemptFinalizeCommand.success(completed.identity, fixed_clock.now(), latency_ms=1)
    )

    with pytest.raises(StartConflictError):
        _ = await repository.reserve_attempt(completed_command)

    partial_command = _start(fixed_clock, request_id="partial-no-reissue")
    partial = await repository.reserve_attempt(partial_command)
    async with routing_session_factory.begin() as session:
        receipt = await session.get(
            UpstreamAttemptReceiptRow,
            partial.identity.started_event_id,
        )
        event = await session.get(AdminEventRow, partial.identity.started_event_id)
        pin = await session.get(UpstreamLivePinRow, partial.identity.started_event_id)
        assert receipt is not None
        assert event is not None
        assert pin is not None
        assert pending_start_matches(receipt, None, pin, partial_command) is False
        assert pending_start_matches(receipt, event, None, partial_command) is False
        await session.delete(pin)

    with pytest.raises(StartConflictError):
        _ = await repository.reserve_attempt(partial_command)
