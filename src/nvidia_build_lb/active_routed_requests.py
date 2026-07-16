"""Process-local linearization guard for active public routed requests."""

import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field


class ActiveRequestAlreadyRegisteredError(RuntimeError):
    """Reject an impossible duplicate server-owned request identity."""


@dataclass(slots=True)
class ActiveRoutedRequestRegistry:
    """Track public request IDs across routing and stream handoff."""

    _request_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def register(self, request_id: str) -> None:
        """Register one server-owned logical request before its first reservation."""
        with self._lock:
            if request_id in self._request_ids:
                raise ActiveRequestAlreadyRegisteredError
            self._request_ids.add(request_id)

    def release(self, request_id: str) -> None:
        """Release one logical request only after no later attempt can start."""
        with self._lock:
            self._request_ids.discard(request_id)

    def contains(self, request_id: str) -> bool:
        """Return a synchronized membership snapshot for maintenance."""
        with self._lock:
            return request_id in self._request_ids

    def snapshot(self) -> frozenset[str]:
        """Return an immutable synchronized diagnostic snapshot."""
        with self._lock:
            return frozenset(self._request_ids)

    @contextmanager
    def track(self, request_id: str) -> Generator[None]:
        """Register and reliably release one request around its ownership window."""
        self.register(request_id)
        try:
            yield
        finally:
            self.release(request_id)
