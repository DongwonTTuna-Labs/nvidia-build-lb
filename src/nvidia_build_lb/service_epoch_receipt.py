"""Same-directory atomic watchdog receipt commit and fresh readback."""

import hashlib
import hmac
import os
import stat
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, override
from uuid import uuid4

from anyio import run_process

_MAX_RECEIPT_BYTES: Final = 4096
_RECEIPT_MODE: Final = 0o600
_WORKER_WRITE_FAILURE: Final = 2
_WORKER_FSYNC_FAILURE: Final = 3
_WORKER_READBACK_FAILURE: Final = 4


class ReceiptWriteError(Exception):
    """The receipt could not be written or atomically replaced."""

    @override
    def __str__(self) -> str:
        return "watchdog_receipt_write_failure"


class ReceiptFsyncError(Exception):
    """The file or parent-directory durability barrier failed."""

    @override
    def __str__(self) -> str:
        return "watchdog_receipt_fsync_failure"


class ReceiptReadbackError(Exception):
    """Fresh strict readback did not equal the committed receipt."""

    @override
    def __str__(self) -> str:
        return "watchdog_receipt_readback_failure"


@dataclass(frozen=True, slots=True)
class AtomicReceiptStore:
    """Commit one mode-0600 receipt with both fsync barriers."""

    path: Path
    require_root_owner: bool = True

    async def commit(self, payload: bytes) -> bytes:
        """Run receipt I/O in a killable child so the outer deadline is real."""
        if not self.path.is_absolute():
            raise ReceiptWriteError
        completed: subprocess.CompletedProcess[bytes] | None = None
        try:
            completed = await run_process(
                [
                    sys.executable,
                    "-m",
                    "nvidia_build_lb.service_epoch_receipt_worker",
                    str(self.path),
                    "1" if self.require_root_owner else "0",
                ],
                input=payload,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
                cwd="/",
                env={},
            )
        except OSError:
            failed = True
        else:
            failed = False
        if failed or completed is None:
            raise ReceiptWriteError
        if completed.returncode == _WORKER_WRITE_FAILURE:
            raise ReceiptWriteError
        if completed.returncode == _WORKER_FSYNC_FAILURE:
            raise ReceiptFsyncError
        if completed.returncode == _WORKER_READBACK_FAILURE:
            raise ReceiptReadbackError
        if completed.returncode != 0:
            raise ReceiptWriteError
        return completed.stdout

    def commit_locally(self, payload: bytes) -> bytes:
        """Worker-only synchronous atomic commit and readback implementation."""
        if not self.path.is_absolute() or not payload or len(payload) > _MAX_RECEIPT_BYTES:
            raise ReceiptWriteError
        parent = self.path.parent
        try:
            parent_stat = parent.stat(follow_symlinks=False)
        except OSError:
            raise ReceiptWriteError from None
        if not stat.S_ISDIR(parent_stat.st_mode) or (
            self.require_root_owner and parent_stat.st_uid != 0
        ):
            raise ReceiptWriteError
        temporary = parent / f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        try:
            self._write_file(temporary, payload)
            try:
                _ = temporary.replace(self.path)
            except OSError:
                raise ReceiptWriteError from None
            self._fsync_directory(parent)
            return self._readback(payload)
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_file(path: Path, payload: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, _RECEIPT_MODE)
        except OSError:
            raise ReceiptWriteError from None
        try:
            os.fchmod(descriptor, _RECEIPT_MODE)
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written < 1:
                    raise ReceiptWriteError
                offset += written
            try:
                os.fsync(descriptor)
            except OSError:
                raise ReceiptFsyncError from None
        except OSError:
            raise ReceiptWriteError from None
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(parent: Path) -> None:
        try:
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            raise ReceiptFsyncError from None
        try:
            os.fsync(descriptor)
        except OSError:
            raise ReceiptFsyncError from None
        finally:
            os.close(descriptor)

    def _readback(self, expected: bytes) -> bytes:
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError:
            raise ReceiptReadbackError from None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != _RECEIPT_MODE
            ):
                raise ReceiptReadbackError
            payload = os.read(descriptor, _MAX_RECEIPT_BYTES + 1)
        except OSError:
            raise ReceiptReadbackError from None
        finally:
            os.close(descriptor)
        if len(payload) > _MAX_RECEIPT_BYTES or not hmac.compare_digest(payload, expected):
            raise ReceiptReadbackError
        if hashlib.sha256(payload).digest() != hashlib.sha256(expected).digest():
            raise ReceiptReadbackError
        return payload
