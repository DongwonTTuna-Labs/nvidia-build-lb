"""Idempotent PostgreSQL reservation and terminal transactions."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.attempt_commit import (
    AttemptCommitUnresolvedError,
    StartConflictError,
    TerminalConflictError,
    finalize_with_reconciliation,
    reserve_with_reconciliation,
)
from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.attempt_reconciliation import reconcile_start, reconcile_terminal
from nvidia_build_lb.attempt_records import (
    complete_receipt,
    completed_terminal_matches,
    identity_matches,
    live_pin,
    pending_receipt,
    pending_start_matches,
    started_event,
    terminal_event,
)
from nvidia_build_lb.attempt_selection import select_attempt_key
from nvidia_build_lb.attempt_transitions import apply_terminal_transition
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptLease,
    AttemptStartCommand,
    StartReconciliation,
    TerminalCommitted,
    TerminalReconciliation,
)
from nvidia_build_lb.credential_types import Clock, ResourceNotFoundError
from nvidia_build_lb.db_models import (
    AdminEventRow,
    SchedulerStateRow,
    UpstreamAttemptReceiptRow,
    UpstreamKeyRow,
    UpstreamLivePinRow,
)
from nvidia_build_lb.quarantine_recovery import terminal_updates_routing_state
from nvidia_build_lb.scheduler_lock import lock_scheduler_state
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.service_epoch_types import EpochSqlConnection, PriorEpochCleanup
from nvidia_build_lb.vault import Vault, VaultEnvelope


@dataclass(frozen=True, slots=True)
class AttemptRepositoryDependencies:
    """Persistence, vault, and time dependencies."""

    sessions: async_sessionmaker[AsyncSession]
    vault: Vault
    clock: Clock
    fail_stop: AttemptFailStop


@dataclass(frozen=True, slots=True)
class AttemptRepository:
    """Own durable routing state without performing NVIDIA network I/O."""

    dependencies: AttemptRepositoryDependencies

    async def reserve_attempt(self, command: AttemptStartCommand) -> AttemptLease:
        """Decrypt then atomically reserve one exact key and live pin."""
        try:
            return await reserve_with_reconciliation(
                lambda: self._reserve_once(command),
                lambda: self.reconcile_start(command),
            )
        except AttemptCommitUnresolvedError:
            self.dependencies.fail_stop.trigger()
            raise

    async def finalize_attempt(
        self,
        command: AttemptFinalizeCommand,
    ) -> TerminalCommitted:
        """Commit an exact terminal receipt, event, transition, and pin delete."""
        try:
            return await finalize_with_reconciliation(
                lambda: self._finalize_once(command),
                lambda: self.reconcile_terminal(command),
            )
        except AttemptCommitUnresolvedError:
            self.dependencies.fail_stop.trigger()
            raise

    async def _reserve_once(self, command: AttemptStartCommand) -> AttemptLease:
        async with self.dependencies.sessions.begin() as session:
            scheduler = await lock_scheduler_state(session)
            existing = await session.get(
                UpstreamAttemptReceiptRow,
                command.started_event_id,
                with_for_update=True,
            )
            if existing is not None:
                return await self._existing_lease(session, existing, command)
            key = await select_attempt_key(
                session,
                scheduler,
                command,
                self.dependencies.clock.now(),
            )
            credential = self.dependencies.vault.decrypt(
                key.id,
                VaultEnvelope(key.vault_version, key.vault_nonce, key.vault_ciphertext),
            )
            await self._persist_start(session, scheduler, key, command)
            return AttemptLease(
                command.identity,
                key.id,
                credential,
                key.consecutive_rate_limits,
                key.consecutive_transient_failures,
            )

    async def _finalize_once(self, command: AttemptFinalizeCommand) -> TerminalCommitted:
        async with self.dependencies.sessions.begin() as session:
            _ = await lock_scheduler_state(session)
            receipt = await session.get(
                UpstreamAttemptReceiptRow,
                command.identity.started_event_id,
                with_for_update=True,
            )
            if receipt is None or not identity_matches(receipt, command.identity):
                raise TerminalConflictError
            key = await session.get(UpstreamKeyRow, receipt.upstream_key_id, with_for_update=True)
            if receipt.terminal_outcome is not None:
                terminal = await session.get(
                    AdminEventRow,
                    command.identity.terminal_event_id,
                    with_for_update=True,
                )
                pin = await session.get(
                    UpstreamLivePinRow,
                    command.identity.started_event_id,
                    with_for_update=True,
                )
                if not completed_terminal_matches(receipt, terminal, pin, command):
                    raise TerminalConflictError
                return TerminalCommitted(command.identity)
            if key is None:
                raise ResourceNotFoundError(
                    resource="upstream_key",
                    resource_id=receipt.upstream_key_id,
                )
            key.success_count += int(command.outcome is TerminalOutcome.SUCCEEDED)
            key.failure_count += int(command.outcome is not TerminalOutcome.SUCCEEDED)
            explicit_probe = receipt.explicit_probe_key_id == key.id
            if terminal_updates_routing_state(
                key,
                command.status_class,
                explicit_probe=explicit_probe,
            ):
                key.last_status_class = command.status_class.value
            key.updated_at = command.terminal_committed_at
            apply_terminal_transition(
                key,
                command,
                explicit_probe=explicit_probe,
            )
            complete_receipt(receipt, command)
            session.add(terminal_event(receipt, command))
            pin = await session.get(UpstreamLivePinRow, receipt.started_event_id)
            if pin is not None:
                await session.delete(pin)
        return TerminalCommitted(command.identity)

    async def reconcile_start(self, command: AttemptStartCommand) -> StartReconciliation:
        """Resolve an ambiguous reservation using one fresh read-only session."""
        return await reconcile_start(
            self.dependencies.sessions,
            self.dependencies.vault,
            command,
        )

    async def reconcile_terminal(
        self,
        command: AttemptFinalizeCommand,
    ) -> TerminalReconciliation:
        """Resolve an ambiguous terminal commit using one fresh read-only session."""
        return await reconcile_terminal(self.dependencies.sessions, command)

    async def cleanup_prior_epoch_pins(
        self,
        connection: EpochSqlConnection,
        active_epoch: UUID,
    ) -> PriorEpochCleanup:
        """Delete prior pins on the already locked dedicated connection."""
        deleted_count = await connection.delete_prior_epoch_pins(active_epoch)
        return PriorEpochCleanup(deleted_count)

    async def _existing_lease(
        self,
        session: AsyncSession,
        receipt: UpstreamAttemptReceiptRow,
        command: AttemptStartCommand,
    ) -> AttemptLease:
        event = await session.get(AdminEventRow, command.started_event_id)
        pin = await session.get(UpstreamLivePinRow, command.started_event_id)
        if not pending_start_matches(receipt, event, pin, command):
            raise StartConflictError
        key = await session.get(
            UpstreamKeyRow,
            receipt.upstream_key_id,
            with_for_update=True,
        )
        if key is None:
            raise StartConflictError
        credential = self.dependencies.vault.decrypt(
            key.id,
            VaultEnvelope(key.vault_version, key.vault_nonce, key.vault_ciphertext),
        )
        return AttemptLease(
            command.identity,
            key.id,
            credential,
            receipt.rate_limit_streak,
            receipt.transient_failure_streak,
        )

    @staticmethod
    async def _persist_start(
        session: AsyncSession,
        scheduler: SchedulerStateRow,
        key: UpstreamKeyRow,
        command: AttemptStartCommand,
    ) -> None:
        key.request_count += 1
        key.last_used_at = command.started_at
        key.updated_at = command.started_at
        scheduler.cursor_key_id = key.id
        scheduler.updated_at = command.started_at
        session.add(started_event(key.id, command))
        session.add(
            pending_receipt(
                key.id,
                command,
                rate_limit_streak=key.consecutive_rate_limits,
                transient_failure_streak=key.consecutive_transient_failures,
            )
        )
        await session.flush()
        session.add(live_pin(key.id, command))
