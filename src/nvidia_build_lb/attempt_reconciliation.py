"""Fresh read-only reconciliation for ambiguous attempt commits."""

import anyio
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nvidia_build_lb.attempt_records import (
    completed_terminal_matches,
    identity_matches,
    pending_start_matches,
)
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptLease,
    AttemptStartCommand,
    StartAbsent,
    StartCommitted,
    StartConflict,
    StartReconciliation,
    StartUnknown,
    TerminalAbsent,
    TerminalConflict,
    TerminalExact,
    TerminalPending,
    TerminalReconciliation,
    TerminalUnknown,
)
from nvidia_build_lb.db_models import (
    AdminEventRow,
    UpstreamAttemptReceiptRow,
    UpstreamKeyRow,
    UpstreamLivePinRow,
)
from nvidia_build_lb.vault import Vault, VaultDecryptionError, VaultEnvelope

_RECONCILE_BUDGET_SECONDS = 5


async def reconcile_start(
    sessions: async_sessionmaker[AsyncSession],
    vault: Vault,
    command: AttemptStartCommand,
) -> StartReconciliation:
    """Classify one ambiguous start from a fresh session without mutation."""
    try:
        with anyio.fail_after(_RECONCILE_BUDGET_SECONDS):
            async with sessions() as session:
                return await _read_start(session, vault, command)
    except (SQLAlchemyError, TimeoutError, VaultDecryptionError):
        return StartUnknown()


async def reconcile_terminal(
    sessions: async_sessionmaker[AsyncSession],
    command: AttemptFinalizeCommand,
) -> TerminalReconciliation:
    """Classify one ambiguous terminal commit from a fresh read-only session."""
    try:
        with anyio.fail_after(_RECONCILE_BUDGET_SECONDS):
            async with sessions() as session:
                return await _read_terminal(session, command)
    except (SQLAlchemyError, TimeoutError):
        return TerminalUnknown()


async def _read_start(
    session: AsyncSession,
    vault: Vault,
    command: AttemptStartCommand,
) -> StartReconciliation:
    receipt = await session.get(UpstreamAttemptReceiptRow, command.started_event_id)
    event = await session.get(AdminEventRow, command.started_event_id)
    pin = await session.get(UpstreamLivePinRow, command.started_event_id)
    if receipt is None:
        return StartAbsent() if event is None and pin is None else StartConflict()
    if not pending_start_matches(receipt, event, pin, command):
        return StartConflict()
    key = await session.get(UpstreamKeyRow, receipt.upstream_key_id)
    if key is None:
        return StartConflict()
    credential = vault.decrypt(
        key.id,
        VaultEnvelope(key.vault_version, key.vault_nonce, key.vault_ciphertext),
    )
    return StartCommitted(
        AttemptLease(
            command.identity,
            key.id,
            credential,
            receipt.rate_limit_streak,
            receipt.transient_failure_streak,
        )
    )


async def _read_terminal(
    session: AsyncSession,
    command: AttemptFinalizeCommand,
) -> TerminalReconciliation:
    receipt = await session.get(
        UpstreamAttemptReceiptRow,
        command.identity.started_event_id,
    )
    terminal_event = await session.get(AdminEventRow, command.identity.terminal_event_id)
    if receipt is None:
        return TerminalAbsent() if terminal_event is None else TerminalConflict()
    if not identity_matches(receipt, command.identity):
        return TerminalConflict()
    pin = await session.get(UpstreamLivePinRow, command.identity.started_event_id)
    if receipt.terminal_outcome is None:
        if terminal_event is not None or pin is None:
            return TerminalConflict()
        return TerminalPending(command.identity)
    exact = completed_terminal_matches(receipt, terminal_event, pin, command)
    return TerminalExact(command.identity) if exact else TerminalConflict()
