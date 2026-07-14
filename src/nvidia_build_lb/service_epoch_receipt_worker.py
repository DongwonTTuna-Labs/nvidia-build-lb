"""Killable subprocess entrypoint for one atomic watchdog receipt."""

import sys
from pathlib import Path

from nvidia_build_lb.service_epoch_receipt import (
    AtomicReceiptStore,
    ReceiptFsyncError,
    ReceiptReadbackError,
    ReceiptWriteError,
)

_ARG_COUNT = 3
_WRITE_FAILURE = 2
_FSYNC_FAILURE = 3
_READBACK_FAILURE = 4


def main() -> int:
    """Commit stdin bytes and emit only verified readback bytes."""
    if len(sys.argv) != _ARG_COUNT or sys.argv[2] not in {"0", "1"}:
        return _WRITE_FAILURE
    payload = sys.stdin.buffer.read(4097)
    store = AtomicReceiptStore(Path(sys.argv[1]), require_root_owner=sys.argv[2] == "1")
    try:
        readback = store.commit_locally(payload)
    except ReceiptFsyncError:
        return _FSYNC_FAILURE
    except ReceiptReadbackError:
        return _READBACK_FAILURE
    except ReceiptWriteError:
        return _WRITE_FAILURE
    _ = sys.stdout.buffer.write(readback)
    _ = sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
