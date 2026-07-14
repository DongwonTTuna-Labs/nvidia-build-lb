"""External cleanup watchdog receipt classification without service mutations."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, unique
from uuid import UUID

from nvidia_build_lb.service_epoch_cleanup import ExitDisposition

_EXIT_RECEIPT_FAILURE = 76
_EXIT_OBSERVATION_TIMEOUT = 77
_EXIT_MISSING_DISPOSITION = 78


@unique
class WatchdogFailure(StrEnum):
    """Closed external observation and durable-receipt failure injections."""

    NONE = "none"
    OBSERVATION_TIMEOUT = "observation_timeout"
    WRITE_FAILURE = "receipt_write_failure"
    WRITE_TIMEOUT = "receipt_write_timeout"
    FSYNC_FAILURE = "receipt_fsync_failure"
    READBACK_FAILURE = "receipt_readback_failure"


@dataclass(frozen=True, slots=True)
class ExternalWatchdogReceipt:
    """Separate atomic receipt written only by the external watchdog."""

    epoch_id: UUID
    nonce: UUID
    verdict: str
    child_exit_status: int
    postgres_backend_absent: bool | None
    advisory_lock_absent: bool | None
    service_transition_writes: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class WatchdogResult:
    """Candidate, durable receipt, and watchdog exit status."""

    candidate: ExternalWatchdogReceipt
    durable_receipt: ExternalWatchdogReceipt | None
    exit_status: int


@dataclass(frozen=True, slots=True)
class WatchdogObservation:
    """Complete child, disposition, probe, and failure observation."""

    disposition: ExitDisposition | None
    epoch_id: UUID
    nonce: UUID
    child_exit_status: int
    observed_at: datetime
    failure: WatchdogFailure = WatchdogFailure.NONE
    missing_disposition_reason: str = "missing_disposition"
    absence_confirmed: bool | None = None


@dataclass(frozen=True, slots=True)
class CleanupExitWatchdog:
    """Classify child/lock/backend observations and receipt durability."""

    def evaluate(
        self,
        observed: WatchdogObservation,
    ) -> WatchdogResult:
        """Build a secret-free receipt without writing a service transition."""
        if observed.failure is WatchdogFailure.OBSERVATION_TIMEOUT:
            candidate = _receipt(
                observed,
                observed.failure.value,
                backend_absent=None,
                lock_absent=None,
            )
            return WatchdogResult(candidate, candidate, _EXIT_OBSERVATION_TIMEOUT)
        if observed.failure is not WatchdogFailure.NONE:
            absent = None if observed.absence_confirmed is False else True
            candidate = _receipt(
                observed,
                observed.failure.value,
                backend_absent=absent,
                lock_absent=absent,
            )
            return WatchdogResult(candidate, None, _EXIT_RECEIPT_FAILURE)
        if observed.disposition is None:
            candidate = _receipt(
                observed,
                observed.missing_disposition_reason,
                backend_absent=True,
                lock_absent=True,
            )
            return WatchdogResult(candidate, candidate, _EXIT_MISSING_DISPOSITION)
        verdict = (
            _verdict(observed.disposition)
            if observed.failure is WatchdogFailure.NONE
            else observed.failure.value
        )
        candidate = _receipt(
            observed,
            verdict,
            backend_absent=True,
            lock_absent=True,
        )
        return WatchdogResult(candidate, candidate, 0)


def _verdict(disposition: ExitDisposition) -> str:
    if disposition.primary_reason == "late_fatal":
        return "late_fatal"
    if disposition.exit_status:
        return "cleanup_failed"
    return "clean"


def _receipt(
    observed: WatchdogObservation,
    verdict: str,
    *,
    backend_absent: bool | None,
    lock_absent: bool | None,
) -> ExternalWatchdogReceipt:
    return ExternalWatchdogReceipt(
        epoch_id=observed.epoch_id,
        nonce=observed.nonce,
        verdict=verdict,
        child_exit_status=observed.child_exit_status,
        postgres_backend_absent=backend_absent,
        advisory_lock_absent=lock_absent,
        service_transition_writes=0,
        observed_at=observed.observed_at,
    )
