"""Single-worker mutation settlement generation barrier."""

import threading
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class MutationBarrierSample:
    """Atomic active-count and generation observation."""

    active_count: int
    generation: int


@dataclass(slots=True)
class AdminMutationBarrier:
    """Track every authenticated admin mutation through response buffering."""

    _active_count: int = field(default=0, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def enter(self) -> None:
        """Enter one mutation and advance the settlement generation."""
        with self._lock:
            self._active_count += 1
            self._generation += 1

    def exit(self) -> None:
        """Exit one mutation in a finally path and advance generation again."""
        with self._lock:
            if self._active_count < 1:
                raise RuntimeError
            self._active_count -= 1
            self._generation += 1

    def sample(self) -> MutationBarrierSample:
        """Return one atomic settlement observation."""
        with self._lock:
            return MutationBarrierSample(self._active_count, self._generation)
