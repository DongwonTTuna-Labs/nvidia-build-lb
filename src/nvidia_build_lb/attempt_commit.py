"""One-attempt commit resolution inside a shared monotonic budget."""

from collections.abc import Awaitable, Callable
from typing import override

import anyio
from sqlalchemy.exc import SQLAlchemyError

from nvidia_build_lb.attempt_types import (
    AttemptLease,
    StartCommitted,
    StartConflict,
    StartReconciliation,
    TerminalCommitted,
    TerminalConflict,
    TerminalExact,
    TerminalReconciliation,
)

_COMMIT_BUDGET_SECONDS = 5


class StartConflictError(Exception):
    """The stable start IDs already name a different command."""


class TerminalConflictError(Exception):
    """The stable terminal IDs already name a different terminal payload."""


class AttemptCommitUnresolvedError(Exception):
    """The shared commit budget expired without a safe durable classification."""

    @override
    def __str__(self) -> str:
        return "attempt_commit_unresolved"


async def reserve_with_reconciliation(
    operation: Callable[[], Awaitable[AttemptLease]],
    reconcile: Callable[[], Awaitable[StartReconciliation]],
) -> AttemptLease:
    """Attempt one reservation and reconcile, but never replay, an ambiguous commit."""
    failure: Exception | None = None
    try:
        with anyio.fail_after(_COMMIT_BUDGET_SECONDS):
            try:
                return await operation()
            except SQLAlchemyError:
                try:
                    reconciled = await reconcile()
                except (SQLAlchemyError, TimeoutError):
                    failure = AttemptCommitUnresolvedError()
                else:
                    if isinstance(reconciled, StartCommitted):
                        return reconciled.lease
                    if isinstance(reconciled, StartConflict):
                        failure = StartConflictError()
                    else:
                        failure = AttemptCommitUnresolvedError()
    except TimeoutError:
        failure = AttemptCommitUnresolvedError()
    raise failure


async def finalize_with_reconciliation(
    operation: Callable[[], Awaitable[TerminalCommitted]],
    reconcile: Callable[[], Awaitable[TerminalReconciliation]],
) -> TerminalCommitted:
    """Attempt one finalization and reconcile, but never replay, an ambiguous commit."""
    failure: Exception | None = None
    try:
        with anyio.fail_after(_COMMIT_BUDGET_SECONDS):
            try:
                return await operation()
            except SQLAlchemyError:
                try:
                    reconciled = await reconcile()
                except (SQLAlchemyError, TimeoutError):
                    failure = AttemptCommitUnresolvedError()
                else:
                    if isinstance(reconciled, TerminalExact):
                        return TerminalCommitted(reconciled.identity)
                    if isinstance(reconciled, TerminalConflict):
                        failure = TerminalConflictError()
                    else:
                        failure = AttemptCommitUnresolvedError()
    except TimeoutError:
        failure = AttemptCommitUnresolvedError()
    raise failure
