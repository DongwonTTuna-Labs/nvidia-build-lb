"""Concrete seqpacket, PostgreSQL absence, and atomic watchdog receipts."""

from __future__ import annotations

import os
import socket
import stat
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Never
from uuid import UUID

import anyio
import pytest
from anyio.to_thread import run_sync

import nvidia_build_lb.service_epoch_receipt as receipt_module
from nvidia_build_lb.service_epoch import (
    ADVISORY_LOCK_KEY_ONE,
    ADVISORY_LOCK_KEY_TWO,
    ServiceEpochRuntime,
    run_supervised_epoch_subprocess,
)
from nvidia_build_lb.service_epoch_cleanup import (
    CleanupDependencies,
    ExitDisposition,
    ExitKind,
    HandoffResult,
)
from nvidia_build_lb.service_epoch_connection import PsycopgEpochConnectionFactory
from nvidia_build_lb.service_epoch_handoff import (
    DispositionProtocolError,
    ReceivedDisposition,
    SeqpacketDispositionReceiver,
    SeqpacketDispositionSender,
    decode_disposition,
    disposition_socketpair,
    encode_disposition,
)
from nvidia_build_lb.service_epoch_monitor import CleanStopState
from nvidia_build_lb.service_epoch_receipt import (
    AtomicReceiptStore,
    ReceiptFsyncError,
    ReceiptReadbackError,
    ReceiptWriteError,
)
from nvidia_build_lb.service_epoch_watchdog import WatchdogFailure, WatchdogResult
from nvidia_build_lb.service_epoch_watchdog_postgres import PostgresEpochAbsenceProbe
from nvidia_build_lb.service_epoch_watchdog_runtime import (
    CleanupWatchdogRuntime,
    PidChildExitWaiter,
    receipt_matches,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import SecretStr

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]

_EPOCH = UUID("00000000-0000-4000-8000-000000000401")
_NONCE = UUID("00000000-0000-4000-8000-000000000402")


class _Connection:
    autocommit: bool = True
    dedicated: bool = True
    driver: str = "psycopg_async"
    pool: str = "none"

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        return True

    async def delete_prior_epoch_pins(self, active_epoch: UUID) -> int:
        del active_epoch
        return 0

    async def ping(self) -> bool:
        return True

    async def advisory_unlock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        return True

    async def close(self) -> None:
        return


class _Committer:
    async def commit_stopping(self, epoch: UUID) -> None:
        assert epoch == _EPOCH


class _Terminator:
    def exit(self, status: int) -> Never:
        raise SystemExit(status)


class _LateFatal:
    def present(self) -> bool:
        return False


class _Child:
    done: anyio.Event
    status: int

    def __init__(self) -> None:
        self.done = anyio.Event()
        self.status = 0

    async def wait(self) -> int:
        await self.done.wait()
        return self.status


class _Probe:
    calls: int = 0

    async def observe(self) -> tuple[bool, bool]:
        self.calls += 1
        return True, True


class _NeverAbsentProbe:
    async def observe(self) -> tuple[bool, bool]:
        return False, False


class _FailingProbe:
    async def observe(self) -> tuple[bool, bool]:
        raise OSError


class _FailingStore:
    failure: WatchdogFailure

    def __init__(self, failure: WatchdogFailure) -> None:
        self.failure = failure

    async def commit(self, payload: bytes) -> bytes:
        del payload
        if self.failure is WatchdogFailure.WRITE_TIMEOUT:
            await anyio.sleep_forever()
        if self.failure is WatchdogFailure.FSYNC_FAILURE:
            raise ReceiptFsyncError
        if self.failure is WatchdogFailure.READBACK_FAILURE:
            raise ReceiptReadbackError
        raise ReceiptWriteError


class _EchoStore:
    async def commit(self, payload: bytes) -> bytes:
        return payload


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 7, 13, 0, 0, 5, tzinfo=UTC)


def _disposition() -> ExitDisposition:
    return ExitDisposition(
        kind=ExitKind.STOPPED,
        mode="clean",
        primary_reason=None,
        cleanup_causes=(),
        exit_status=0,
        epoch_id=_EPOCH,
        nonce=_NONCE,
    )


def _rejected_disposition() -> ExitDisposition:
    return ExitDisposition(
        kind=ExitKind.STOPPED,
        mode="clean",
        primary_reason=None,
        cleanup_causes=(),
        exit_status=0,
        epoch_id=UUID("00000000-0000-4000-8000-000000000499"),
        nonce=_NONCE,
    )


def _runtime(connection: _Connection) -> ServiceEpochRuntime:
    joined = anyio.Event()
    joined.set()
    return ServiceEpochRuntime(
        epoch=_EPOCH,
        connection=connection,
        clean_stop=CleanStopState(),
        monitor_scope=anyio.CancelScope(),
        monitor_joined=joined,
    )


async def _run_watchdog_with_store(
    store: _FailingStore | _EchoStore,
    disposition_mode: str = "valid",
    probe: _Probe | _NeverAbsentProbe | None = None,
) -> WatchdogResult:
    service_channel, watchdog_channel = disposition_socketpair()
    child = _Child()
    child.done.set()
    sender = SeqpacketDispositionSender(service_channel)
    result: WatchdogResult | None = None

    async def send() -> None:
        if disposition_mode == "missing":
            return
        disposition = _disposition() if disposition_mode == "valid" else _rejected_disposition()
        expected = HandoffResult.ACK if disposition_mode == "valid" else HandoffResult.NACK
        assert await sender.send(disposition) is expected

    try:
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(send)
            result = await CleanupWatchdogRuntime(
                receiver=SeqpacketDispositionReceiver(watchdog_channel, timeout_seconds=0.01),
                child=child,
                probe=probe or _Probe(),
                receipt_store=store,
                clock=_Clock(),
                receipt_timeout_seconds=0.01,
                observation_timeout_seconds=0.01,
            ).run(epoch_id=_EPOCH, nonce=_NONCE)
    finally:
        service_channel.close()
        watchdog_channel.close()
    assert result is not None
    return result


def test_disposition_protocol_is_canonical_and_rejects_duplicates() -> None:
    packet = encode_disposition(_disposition())

    assert len(packet) <= 4096
    assert decode_disposition(packet) == _disposition()
    duplicate = packet.replace(b'"mode":"clean"', b'"mode":"clean","mode":"clean"')
    with pytest.raises(DispositionProtocolError):
        _ = decode_disposition(duplicate)

    invalid_mode = packet.replace(b'"mode":"clean"', b'"mode":"invalid"')
    with pytest.raises(DispositionProtocolError):
        _ = decode_disposition(invalid_mode)

    failed = ExitDisposition(
        kind=ExitKind.TERMINATING,
        mode="clean_cleanup_failed",
        primary_reason="close_exception",
        cleanup_causes=("close_exception",),
        exit_status=74,
        epoch_id=_EPOCH,
        nonce=_NONCE,
    )
    assert decode_disposition(encode_disposition(failed)) == failed
    wrong_priority = ExitDisposition(
        kind=ExitKind.TERMINATING,
        mode="clean_cleanup_failed",
        primary_reason="unlock_false",
        cleanup_causes=("unlock_false", "close_exception"),
        exit_status=72,
        epoch_id=_EPOCH,
        nonce=_NONCE,
    )
    with pytest.raises(DispositionProtocolError):
        _ = decode_disposition(encode_disposition(wrong_priority))


async def test_real_seqpacket_cleanup_and_atomic_receipt(tmp_path: Path) -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    child = _Child()
    probe = _Probe()
    receipt_path = tmp_path / "cleanup.json"
    watchdog_result: list[WatchdogResult] = []
    watchdog = CleanupWatchdogRuntime(
        receiver=SeqpacketDispositionReceiver(watchdog_channel),
        child=child,
        probe=probe,
        receipt_store=AtomicReceiptStore(receipt_path, require_root_owner=False),
        clock=_Clock(),
    )

    async def run_watchdog() -> None:
        watchdog_result.append(await watchdog.run(epoch_id=_EPOCH, nonce=_NONCE))

    try:
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(run_watchdog)
            cleanup = await run_supervised_epoch_subprocess(
                _runtime(_Connection()),
                CleanupDependencies(
                    nonce=_NONCE,
                    committer=_Committer(),
                    handoff=SeqpacketDispositionSender(service_channel),
                    terminator=_Terminator(),
                    late_fatal=_LateFatal(),
                ),
            )
            assert cleanup.disposition == _disposition()
            child.done.set()
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert len(watchdog_result) == 1
    result = watchdog_result[0]
    assert result.exit_status == 0
    assert result.durable_receipt is not None
    payload = receipt_path.read_bytes()
    assert receipt_matches(payload, result.durable_receipt)
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    assert _temporary_paths(tmp_path) == []
    assert probe.calls == 1


async def test_sender_ack_wait_is_bounded_when_peer_does_not_receive() -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    try:
        sender = SeqpacketDispositionSender(service_channel, timeout_seconds=0.01)
        assert await sender.send(_disposition()) is HandoffResult.ACK_TIMEOUT
    finally:
        service_channel.close()
        watchdog_channel.close()


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(WatchdogFailure.WRITE_FAILURE, id="receipt_write_failure"),
        pytest.param(WatchdogFailure.WRITE_TIMEOUT, id="receipt_write_timeout"),
        pytest.param(WatchdogFailure.FSYNC_FAILURE, id="receipt_fsync_failure"),
        pytest.param(WatchdogFailure.READBACK_FAILURE, id="receipt_readback_failure"),
    ],
)
@pytest.mark.parametrize("disposition_mode", ["valid", "missing", "rejected"])
async def test_watchdog_receipt_failures_have_no_durable_success(
    failure: WatchdogFailure,
    disposition_mode: str,
) -> None:
    result = await _run_watchdog_with_store(_FailingStore(failure), disposition_mode)

    assert result.exit_status == 76
    assert result.durable_receipt is None
    assert result.candidate.verdict == failure.value
    assert result.candidate.postgres_backend_absent is True
    assert result.candidate.advisory_lock_absent is True


@pytest.mark.parametrize("disposition_mode", ["missing", "rejected"])
async def test_disposition_problem_with_observation_timeout_keeps_null_absence(
    disposition_mode: str,
) -> None:
    result = await _run_watchdog_with_store(
        _EchoStore(),
        disposition_mode,
        _NeverAbsentProbe(),
    )

    assert result.exit_status == 77
    assert result.durable_receipt is not None
    assert result.candidate.verdict == WatchdogFailure.OBSERVATION_TIMEOUT.value
    assert result.candidate.postgres_backend_absent is None
    assert result.candidate.advisory_lock_absent is None


async def test_observation_timeout_commits_null_absence_receipt(tmp_path: Path) -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    child = _Child()
    child.done.set()
    receipt_path = tmp_path / "observation-timeout.json"
    result: WatchdogResult | None = None

    async def send() -> None:
        assert (
            await SeqpacketDispositionSender(service_channel).send(_disposition())
            is HandoffResult.ACK
        )

    try:
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(send)
            result = await CleanupWatchdogRuntime(
                receiver=SeqpacketDispositionReceiver(watchdog_channel),
                child=child,
                probe=_NeverAbsentProbe(),
                receipt_store=AtomicReceiptStore(receipt_path, require_root_owner=False),
                clock=_Clock(),
                observation_timeout_seconds=0.01,
            ).run(epoch_id=_EPOCH, nonce=_NONCE)
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert result is not None
    assert result.exit_status == 77
    assert result.durable_receipt is not None
    assert result.durable_receipt.postgres_backend_absent is None
    assert result.durable_receipt.advisory_lock_absent is None


async def test_probe_errors_are_bounded_into_null_absence_receipt(tmp_path: Path) -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    child = _Child()
    child.done.set()
    result: WatchdogResult | None = None

    async def send() -> None:
        assert (
            await SeqpacketDispositionSender(service_channel).send(_disposition())
            is HandoffResult.ACK
        )

    try:
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(send)
            result = await CleanupWatchdogRuntime(
                receiver=SeqpacketDispositionReceiver(watchdog_channel),
                child=child,
                probe=_FailingProbe(),
                receipt_store=AtomicReceiptStore(
                    tmp_path / "probe-error.json",
                    require_root_owner=False,
                ),
                clock=_Clock(),
                observation_timeout_seconds=0.01,
            ).run(epoch_id=_EPOCH, nonce=_NONCE)
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert result is not None
    assert result.exit_status == 77
    assert result.candidate.verdict == "observation_timeout"
    assert result.durable_receipt is not None


async def test_missing_disposition_is_durably_classified(tmp_path: Path) -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    child = _Child()
    child.status = 75
    child.done.set()
    result: WatchdogResult | None = None
    try:
        result = await CleanupWatchdogRuntime(
            receiver=SeqpacketDispositionReceiver(watchdog_channel, timeout_seconds=0.01),
            child=child,
            probe=_Probe(),
            receipt_store=AtomicReceiptStore(
                tmp_path / "missing-disposition.json",
                require_root_owner=False,
            ),
            clock=_Clock(),
        ).run(epoch_id=_EPOCH, nonce=_NONCE)
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert result is not None
    assert result.exit_status == 78
    assert result.candidate.verdict == "missing_disposition"
    assert result.durable_receipt is not None


async def test_child_exit_mismatch_is_durably_rejected(tmp_path: Path) -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    child = _Child()
    child.status = 75
    child.done.set()
    result: WatchdogResult | None = None

    async def send() -> None:
        assert (
            await SeqpacketDispositionSender(service_channel).send(_disposition())
            is HandoffResult.ACK
        )

    try:
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(send)
            result = await CleanupWatchdogRuntime(
                receiver=SeqpacketDispositionReceiver(watchdog_channel),
                child=child,
                probe=_Probe(),
                receipt_store=AtomicReceiptStore(
                    tmp_path / "exit-mismatch.json",
                    require_root_owner=False,
                ),
                clock=_Clock(),
            ).run(epoch_id=_EPOCH, nonce=_NONCE)
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert result is not None
    assert result.exit_status == 78
    assert result.candidate.verdict == "rejected_disposition"
    assert result.candidate.child_exit_status == 75
    assert result.durable_receipt is not None


async def test_receipt_store_missing_parent_fails_closed(tmp_path: Path) -> None:
    store = AtomicReceiptStore(tmp_path / "missing" / "receipt.json", require_root_owner=False)

    with pytest.raises(ReceiptWriteError):
        _ = await store.commit(b"{}")


async def test_receipt_worker_spawn_os_error_maps_to_typed_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_spawn(*_args: object, **_kwargs: object) -> Never:
        raise OSError

    monkeypatch.setattr(receipt_module, "run_process", fail_spawn)
    store = AtomicReceiptStore(tmp_path / "receipt.json", require_root_owner=False)

    with pytest.raises(ReceiptWriteError) as captured:
        _ = await store.commit(b"{}")

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_receipt_file_fsync_failure_leaves_no_committed_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "receipt.json"

    def fail_fsync(descriptor: int) -> None:
        del descriptor
        raise OSError

    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(ReceiptFsyncError):
        _ = AtomicReceiptStore(path, require_root_owner=False).commit_locally(b"{}")
    assert not path.exists()
    assert _temporary_paths(tmp_path) == []


async def test_pid_child_waiter_reaps_exact_process() -> None:
    pid = _spawn_true()

    assert await PidChildExitWaiter(pid).wait() == 0


def _spawn_true() -> int:
    return os.posix_spawn("/bin/true", ["/bin/true"], {})


async def test_postgres_probe_observes_backend_and_lock_absence(
    empty_database: SecretStr,
) -> None:
    connection = await PsycopgEpochConnectionFactory(empty_database).open()
    assert (
        await connection.try_advisory_lock(
            ADVISORY_LOCK_KEY_ONE,
            ADVISORY_LOCK_KEY_TWO,
        )
        is True
    )
    probe = PostgresEpochAbsenceProbe(
        empty_database,
        connection.backend_pid,
        ADVISORY_LOCK_KEY_ONE,
        ADVISORY_LOCK_KEY_TWO,
    )
    assert await probe.observe() == (False, False)
    assert (
        await connection.advisory_unlock(
            ADVISORY_LOCK_KEY_ONE,
            ADVISORY_LOCK_KEY_TWO,
        )
        is True
    )
    await connection.close()
    assert await probe.observe() == (True, True)


async def test_receiver_nacks_mismatched_epoch() -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    sender = SeqpacketDispositionSender(service_channel)
    receiver = SeqpacketDispositionReceiver(watchdog_channel)
    result: list[HandoffResult] = []
    received: ReceivedDisposition | None = None

    async def send() -> None:
        result.append(await sender.send(_disposition()))

    try:
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(send)
            received = await receiver.receive(
                epoch_id=UUID("00000000-0000-4000-8000-000000000499"),
                nonce=_NONCE,
            )
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert received is not None
    assert received.disposition is None
    assert received.rejection == "rejected_disposition"
    assert result == [HandoffResult.NACK]


async def test_receiver_nacks_truncated_seqpacket_as_rejected_disposition() -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    try:
        sent = await run_sync(service_channel.send, b"x" * 4097)
        received = await SeqpacketDispositionReceiver(watchdog_channel).receive(
            epoch_id=_EPOCH,
            nonce=_NONCE,
        )
        reply = await run_sync(service_channel.recv, 4)
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert sent == 4097
    assert reply == b"NACK"
    assert received.disposition is None
    assert received.rejection == "rejected_disposition"


async def test_sender_maps_truncated_ack_packet_to_nack() -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    results: list[HandoffResult] = []

    async def send() -> None:
        results.append(await SeqpacketDispositionSender(service_channel).send(_disposition()))

    async def reply_with_truncated_packet() -> None:
        _ = await run_sync(watchdog_channel.recv, 4096)
        sent = await run_sync(watchdog_channel.send, b"x" * 4097)
        assert sent == 4097

    try:
        async with anyio.create_task_group() as tasks:
            _ = tasks.start_soon(send)
            _ = tasks.start_soon(reply_with_truncated_packet)
    finally:
        service_channel.close()
        watchdog_channel.close()

    assert results == [HandoffResult.NACK]


def test_seqpacket_pair_is_unix_seqpacket_and_close_on_exec() -> None:
    service_channel, watchdog_channel = disposition_socketpair()
    try:
        assert service_channel.family == socket.AF_UNIX
        assert service_channel.type & socket.SOCK_SEQPACKET
        assert not service_channel.get_inheritable()
        assert not watchdog_channel.get_inheritable()
    finally:
        service_channel.close()
        watchdog_channel.close()


def _temporary_paths(directory: Path) -> list[Path]:
    return list(directory.glob(".*.tmp"))
