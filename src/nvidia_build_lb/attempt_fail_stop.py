"""One-shot lifecycle fail-stop for unresolved durable attempt commits."""

import threading
from dataclasses import dataclass, field
from typing import Protocol


class ReadinessGate(Protocol):
    """Withdraw request intake before cancelling the process root."""

    def set_ready(self, ready: bool) -> None:
        """Publish the readiness state synchronously."""
        ...


class RootCancellation(Protocol):
    """Cancel the lifecycle owner after readiness is withdrawn."""

    def cancel(self) -> None:
        """Cancel the root task scope."""
        ...


class AttemptFailStop(Protocol):
    """Closed sink used only for an unresolved durable commit."""

    def trigger(self) -> None:
        """Withdraw readiness and cancel the root exactly once."""
        ...


@dataclass(slots=True)
class LifecycleAttemptFailStop:
    """Order ready-false before one root cancellation under a process lock."""

    readiness: ReadinessGate
    root_cancellation: RootCancellation
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _triggered: bool = field(default=False, init=False, repr=False)
    _readiness_failed: bool = field(default=False, init=False, repr=False)
    _root_cancel_failed: bool = field(default=False, init=False, repr=False)

    def trigger(self) -> None:
        """Perform the fail-stop transition once; caller still propagates the fatal error."""
        with self._lock:
            if self._triggered:
                return
            self._triggered = True
        try:
            self.readiness.set_ready(False)
        except Exception:  # noqa: BLE001 - collaborator details must not replace the fatal.
            with self._lock:
                self._readiness_failed = True
        try:
            self.root_cancellation.cancel()
        except Exception:  # noqa: BLE001 - collaborator details must not replace the fatal.
            with self._lock:
                self._root_cancel_failed = True

    @property
    def triggered(self) -> bool:
        """Return whether this process accepted the fatal transition."""
        with self._lock:
            return self._triggered

    @property
    def collaborator_failure(self) -> bool:
        """Report only a safe boolean when a fail-stop collaborator raised."""
        with self._lock:
            return self._readiness_failed or self._root_cancel_failed
