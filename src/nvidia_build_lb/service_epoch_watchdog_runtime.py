"""Concrete child wait, absence observation, and durable watchdog receipt."""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol
from uuid import UUID

import anyio
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from nvidia_build_lb.service_epoch_handoff import (
    ReceivedDisposition,
    SeqpacketDispositionReceiver,
)
from nvidia_build_lb.service_epoch_receipt import (
    ReceiptFsyncError,
    ReceiptReadbackError,
    ReceiptWriteError,
)
from nvidia_build_lb.service_epoch_watchdog import (
    CleanupExitWatchdog,
    ExternalWatchdogReceipt,
    WatchdogFailure,
    WatchdogObservation,
    WatchdogResult,
)
from nvidia_build_lb.strict_json import load_unique_json


class ChildExitWaiter(Protocol):
    """Observe one child exit without running service callbacks."""

    async def wait(self) -> int:
        """Return one nonnegative shell-style exit status."""
        ...


class EpochAbsenceProbe(Protocol):
    """Observe the service PostgreSQL backend and advisory lock externally."""

    async def observe(self) -> tuple[bool, bool]:
        """Return backend-absent and lock-absent booleans."""
        ...


class WatchdogReceiptStore(Protocol):
    """Commit canonical bytes and return a fresh readback."""

    async def commit(self, payload: bytes) -> bytes:
        """Return only after file and directory fsync plus strict readback."""
        ...


class WatchdogClock(Protocol):
    """Supply the external receipt observation time."""

    def now(self) -> datetime:
        """Return one timezone-aware UTC instant."""
        ...


class _ReceiptDocument(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    advisory_lock_absent: bool | None
    child_exit_status: int
    epoch_id: str
    nonce: str
    observed_at: str
    postgres_backend_absent: bool | None
    service_transition_writes: int
    verdict: str


_RECEIPT_ADAPTER = TypeAdapter(_ReceiptDocument)


@dataclass(frozen=True, slots=True)
class PidChildExitWaiter:
    """Poll waitpid without blocking so the outer deadline remains effective."""

    pid: int
    interval_seconds: float = 0.01

    async def wait(self) -> int:
        """Reap one exact child and normalize signal exits to 128+signal."""
        while True:
            observed_pid, status = _waitpid_nohang(self.pid)
            if observed_pid == self.pid:
                exit_code = os.waitstatus_to_exitcode(status)
                return exit_code if exit_code >= 0 else 128 - exit_code
            await anyio.sleep(self.interval_seconds)


@dataclass(frozen=True, slots=True)
class CleanupWatchdogRuntime:
    """Own the external half of cleanup observation and receipt durability."""

    receiver: SeqpacketDispositionReceiver
    child: ChildExitWaiter
    probe: EpochAbsenceProbe
    receipt_store: WatchdogReceiptStore
    clock: WatchdogClock
    child_timeout_seconds: float = 5.0
    observation_timeout_seconds: float = 5.0
    receipt_timeout_seconds: float = 5.0
    probe_interval_seconds: float = 0.01

    async def run(self, *, epoch_id: UUID, nonce: UUID) -> WatchdogResult:
        """ACK disposition, wait child, prove absence, and atomically receipt."""
        received = await self.receiver.receive(epoch_id=epoch_id, nonce=nonce)
        child_status = await self._wait_child()
        received = _match_child_exit(received, child_status)
        failure = await self._observe_absence()
        observation = WatchdogObservation(
            disposition=received.disposition,
            epoch_id=epoch_id,
            nonce=nonce,
            child_exit_status=child_status,
            observed_at=self.clock.now(),
            failure=failure,
            missing_disposition_reason=received.rejection or "missing_disposition",
            absence_confirmed=failure is WatchdogFailure.NONE,
        )
        candidate = CleanupExitWatchdog().evaluate(observation)
        return await self._commit(observation, candidate)

    async def _wait_child(self) -> int:
        with anyio.fail_after(self.child_timeout_seconds):
            return await self.child.wait()

    async def _observe_absence(self) -> WatchdogFailure:
        try:
            with anyio.fail_after(self.observation_timeout_seconds):
                while True:
                    try:
                        backend_absent, lock_absent = await self.probe.observe()
                    except Exception:  # noqa: BLE001 - only bounded absence failure is retained.
                        await anyio.sleep(self.probe_interval_seconds)
                        continue
                    if backend_absent and lock_absent:
                        return WatchdogFailure.NONE
                    await anyio.sleep(self.probe_interval_seconds)
        except TimeoutError:
            return WatchdogFailure.OBSERVATION_TIMEOUT

    async def _commit(
        self,
        observation: WatchdogObservation,
        result: WatchdogResult,
    ) -> WatchdogResult:
        payload = encode_watchdog_receipt(result.candidate)
        try:
            with anyio.fail_after(self.receipt_timeout_seconds):
                readback = await self.receipt_store.commit(payload)
        except TimeoutError:
            return _failed_receipt(observation, WatchdogFailure.WRITE_TIMEOUT)
        except ReceiptFsyncError:
            return _failed_receipt(observation, WatchdogFailure.FSYNC_FAILURE)
        except ReceiptReadbackError:
            return _failed_receipt(observation, WatchdogFailure.READBACK_FAILURE)
        except ReceiptWriteError:
            return _failed_receipt(observation, WatchdogFailure.WRITE_FAILURE)
        if readback != payload or not receipt_matches(readback, result.candidate):
            return _failed_receipt(observation, WatchdogFailure.READBACK_FAILURE)
        return result


def encode_watchdog_receipt(receipt: ExternalWatchdogReceipt) -> bytes:
    """Encode the exact closed receipt schema as canonical UTF-8 JSON."""
    document = _receipt_document(receipt)
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def receipt_matches(payload: bytes, expected: ExternalWatchdogReceipt) -> bool:
    """Strictly parse exact fields and compare to the typed candidate."""
    try:
        parsed = load_unique_json(payload.decode("utf-8", errors="strict"))
        document = _RECEIPT_ADAPTER.validate_python(parsed)
    except (UnicodeDecodeError, ValueError, ValidationError):
        return False
    return document.model_dump(mode="python") == _receipt_document(expected)


def _failed_receipt(
    observation: WatchdogObservation,
    failure: WatchdogFailure,
) -> WatchdogResult:
    failed = WatchdogObservation(
        disposition=observation.disposition,
        epoch_id=observation.epoch_id,
        nonce=observation.nonce,
        child_exit_status=observation.child_exit_status,
        observed_at=observation.observed_at,
        failure=failure,
        missing_disposition_reason=observation.missing_disposition_reason,
        absence_confirmed=observation.absence_confirmed,
    )
    return CleanupExitWatchdog().evaluate(failed)


def _match_child_exit(received: ReceivedDisposition, child_status: int) -> ReceivedDisposition:
    disposition = received.disposition
    if disposition is None or disposition.exit_status == child_status:
        return received
    return ReceivedDisposition(None, "rejected_disposition")


def _receipt_document(receipt: ExternalWatchdogReceipt) -> dict[str, object]:
    timestamp = receipt.observed_at.isoformat().replace("+00:00", "Z")
    return {
        "advisory_lock_absent": receipt.advisory_lock_absent,
        "child_exit_status": receipt.child_exit_status,
        "epoch_id": str(receipt.epoch_id),
        "nonce": str(receipt.nonce),
        "observed_at": timestamp,
        "postgres_backend_absent": receipt.postgres_backend_absent,
        "service_transition_writes": receipt.service_transition_writes,
        "verdict": receipt.verdict,
    }


def _waitpid_nohang(pid: int) -> tuple[int, int]:
    return os.waitpid(pid, os.WNOHANG)
