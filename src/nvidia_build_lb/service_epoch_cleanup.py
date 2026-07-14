"""Fail-closed service epoch unlock, close, disposition, and watchdog receipts."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Never, Protocol
from uuid import UUID

import anyio

from nvidia_build_lb.async_cleanup import AsyncCleanupUnresolvedError, run_bounded_cleanup
from nvidia_build_lb.service_epoch_types import EpochSqlConnection

_ADVISORY_LOCK_KEY_ONE = 1_312_967_746
_ADVISORY_LOCK_KEY_TWO = 1

_EXIT_UNLOCK_FALSE = 72
_EXIT_UNLOCK_EXCEPTION = 73
_EXIT_CLOSE_EXCEPTION = 74
_EXIT_HANDOFF = 75
_EXIT_LATE_FATAL = 79
CLEANUP_IO_TIMEOUT_SECONDS = 5.0


@unique
class ExitKind(StrEnum):
    """Closed process-local cleanup disposition kinds."""

    STOPPED = "Stopped"
    TERMINATING = "Terminating"


@unique
class HandoffResult(StrEnum):
    """Bounded SOCK_SEQPACKET disposition handoff outcomes."""

    ACK = "ack"
    NACK = "nack"
    SEND_TIMEOUT = "send_timeout"
    ACK_TIMEOUT = "ack_timeout"


@dataclass(frozen=True, slots=True)
class ExitDisposition:
    """Typed process-local cleanup result sent once to the external watchdog."""

    kind: ExitKind
    mode: str
    primary_reason: str | None
    cleanup_causes: tuple[str, ...]
    exit_status: int
    epoch_id: UUID
    nonce: UUID


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Safe counters proving exact-once unlock, close, and handoff attempts."""

    disposition: ExitDisposition
    unlock_attempts: int
    unlock_results: int
    unlock_successes: int
    unlock_exceptions: int
    close_attempts: int
    close_results: int
    close_successes: int
    close_exceptions: int
    handoff_attempts: int
    handoff_acks: int


class StoppingCommitter(Protocol):
    """Commit the last service-owned durable state before close begins."""

    async def commit_stopping(self, epoch: UUID) -> None:
        """Commit one clean stopping observation."""
        ...


class DispositionHandoff(Protocol):
    """Send exactly one typed disposition and await bounded ACK/NACK."""

    async def send(self, disposition: ExitDisposition) -> HandoffResult:
        """Return one safe handoff result."""
        ...


class ProcessTerminator(Protocol):
    """Own the only nonzero immediate process exit."""

    def exit(self, status: int) -> Never:
        """Terminate without running service callbacks."""
        ...


class LateFatalProbe(Protocol):
    """Observe the exact post-close, pre-disposition late fatal checkpoint."""

    def present(self) -> bool:
        """Return whether one late fatal was accepted."""
        ...


class CleanupRuntime(Protocol):
    """Narrow post-drain epoch resources owned by the cleanup coordinator."""

    @property
    def epoch(self) -> UUID:
        """Return the one active process epoch."""
        ...

    @property
    def connection(self) -> EpochSqlConnection:
        """Return the owned advisory-lock connection."""
        ...


@dataclass(frozen=True, slots=True)
class CleanupDependencies:
    """All non-runtime cleanup collaborators and the disposition nonce."""

    nonce: UUID
    committer: StoppingCommitter
    handoff: DispositionHandoff
    terminator: ProcessTerminator
    late_fatal: LateFatalProbe


@dataclass(frozen=True, slots=True)
class _CleanupObservations:
    epoch: UUID
    nonce: UUID
    unlock_result: bool | None
    unlock_exception: bool
    close_exception: bool
    late_fatal: bool


async def cleanup_service_epoch(
    runtime: CleanupRuntime,
    dependencies: CleanupDependencies,
) -> CleanupResult:
    """Commit, unlock, close, hand off, then return or terminate exactly once."""
    await _bounded(lambda: dependencies.committer.commit_stopping(runtime.epoch))
    unlock_result: bool | None = None
    unlock_exception = False
    try:
        unlock_result = await _bounded(
            lambda: runtime.connection.advisory_unlock(
                _ADVISORY_LOCK_KEY_ONE,
                _ADVISORY_LOCK_KEY_TWO,
            )
        )
    except Exception:  # noqa: BLE001 - only a safe cleanup class is retained.
        unlock_exception = True
    close_exception = False
    try:
        await _bounded(runtime.connection.close)
    except Exception:  # noqa: BLE001 - only a safe cleanup class is retained.
        close_exception = True
    disposition = _disposition(
        _CleanupObservations(
            epoch=runtime.epoch,
            nonce=dependencies.nonce,
            unlock_result=unlock_result,
            unlock_exception=unlock_exception,
            close_exception=close_exception,
            late_fatal=dependencies.late_fatal.present(),
        )
    )
    try:
        handoff_result = await _bounded(lambda: dependencies.handoff.send(disposition))
    except Exception:  # noqa: BLE001 - every handoff failure has one deterministic exit.
        dependencies.terminator.exit(_EXIT_HANDOFF)
    if handoff_result is not HandoffResult.ACK:
        dependencies.terminator.exit(_EXIT_HANDOFF)
    if disposition.exit_status:
        dependencies.terminator.exit(disposition.exit_status)
    return CleanupResult(
        disposition=disposition,
        unlock_attempts=1,
        unlock_results=int(not unlock_exception),
        unlock_successes=int(unlock_result is True),
        unlock_exceptions=int(unlock_exception),
        close_attempts=1,
        close_results=int(not close_exception),
        close_successes=int(not close_exception),
        close_exceptions=int(close_exception),
        handoff_attempts=1,
        handoff_acks=1,
    )


async def _bounded[T](operation: Callable[[], Awaitable[T]]) -> T:
    unresolved = False
    try:
        with anyio.CancelScope(shield=True):
            return await run_bounded_cleanup(operation, CLEANUP_IO_TIMEOUT_SECONDS)
    except AsyncCleanupUnresolvedError:
        unresolved = True
    if unresolved:
        raise TimeoutError from None
    raise RuntimeError


def _disposition(observed: _CleanupObservations) -> ExitDisposition:
    causes: list[str] = []
    if observed.unlock_exception:
        causes.append("unlock_exception")
    elif observed.unlock_result is not True:
        causes.append("unlock_false")
    if observed.close_exception:
        causes.append("close_exception")
    if observed.late_fatal:
        causes.append("late_fatal")
    if observed.late_fatal:
        reason, status = "late_fatal", _EXIT_LATE_FATAL
    elif observed.close_exception:
        reason, status = "close_exception", _EXIT_CLOSE_EXCEPTION
    elif observed.unlock_exception:
        reason, status = "unlock_exception", _EXIT_UNLOCK_EXCEPTION
    elif observed.unlock_result is not True:
        reason, status = "unlock_false", _EXIT_UNLOCK_FALSE
    else:
        reason, status = None, 0
    return ExitDisposition(
        kind=ExitKind.STOPPED if status == 0 else ExitKind.TERMINATING,
        mode="clean" if status == 0 else "clean_cleanup_failed",
        primary_reason=reason,
        cleanup_causes=tuple(causes),
        exit_status=status,
        epoch_id=observed.epoch,
        nonce=observed.nonce,
    )
