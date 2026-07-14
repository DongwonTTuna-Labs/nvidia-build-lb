# ruff: noqa: I001
"""Import httpx2 only after the runtime closure and sealed loggers pass."""

from nvidia_build_lb import pinned_runtime as _pinned_runtime

import httpx2
from httpx2._client import BoundAsyncStream
from httpx2._exceptions import request_context

_RUNTIME_RECEIPT = _pinned_runtime.PINNED_RUNTIME

__all__ = ["BoundAsyncStream", "httpx2", "request_context"]
