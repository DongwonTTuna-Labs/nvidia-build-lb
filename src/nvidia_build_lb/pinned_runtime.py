"""Stable facade and startup gate for the pinned H1 provider runtime."""

from typing import Final

from nvidia_build_lb.runtime_guard import TransportBoundaryGuard
from nvidia_build_lb.runtime_loader import load_isolated_runtime
from nvidia_build_lb.runtime_logger import (
    FrozenNoOpLogger,
    install_sealed_loggers,
    provider_logger,
)
from nvidia_build_lb.runtime_manifest import verify_runtime_closure
from nvidia_build_lb.runtime_types import (
    IsolatedRuntime,
    PinnedRuntimeDriftError,
    RuntimeGuardSnapshot,
    RuntimeReceipt,
    SealedLoggerMutationError,
)

PINNED_RUNTIME: Final[RuntimeReceipt] = verify_runtime_closure()
_SEALED_LOGGERS: Final[dict[str, FrozenNoOpLogger]] = install_sealed_loggers()

__all__ = [
    "PINNED_RUNTIME",
    "FrozenNoOpLogger",
    "IsolatedRuntime",
    "PinnedRuntimeDriftError",
    "RuntimeGuardSnapshot",
    "RuntimeReceipt",
    "SealedLoggerMutationError",
    "TransportBoundaryGuard",
    "load_isolated_runtime",
    "provider_logger",
    "verify_runtime_closure",
]
