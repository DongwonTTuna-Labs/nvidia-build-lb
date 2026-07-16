"""Shielded monitor join and final service-epoch cleanup."""

from typing import Protocol
from uuid import UUID

import anyio

from nvidia_build_lb.service_epoch_cleanup import (
    CleanupDependencies,
    CleanupResult,
    cleanup_service_epoch,
)
from nvidia_build_lb.service_epoch_monitor import CleanStopState
from nvidia_build_lb.service_epoch_types import EpochSqlConnection


class SupervisedEpochRuntime(Protocol):
    """Narrow live runtime required by shutdown supervision."""

    @property
    def epoch(self) -> UUID:
        """Return the active epoch."""
        ...

    @property
    def connection(self) -> EpochSqlConnection:
        """Return the advisory-lock connection."""
        ...

    @property
    def clean_stop(self) -> CleanStopState:
        """Return clean-stop state."""
        ...

    @property
    def monitor_scope(self) -> anyio.CancelScope:
        """Return the monitor cancel scope."""
        ...

    @property
    def monitor_joined(self) -> anyio.Event:
        """Return the monitor join event."""
        ...


async def supervise_epoch_cleanup(
    runtime: SupervisedEpochRuntime,
    dependencies: CleanupDependencies,
    *,
    join_timeout_seconds: float,
) -> CleanupResult:
    """Cancel and join the monitor before the final DB/unlock/close sequence."""
    runtime.clean_stop.request(dependencies.nonce)
    runtime.monitor_scope.cancel()
    result: CleanupResult | None = None
    with anyio.CancelScope(shield=True):
        with anyio.fail_after(join_timeout_seconds):
            await runtime.monitor_joined.wait()
        result = await cleanup_service_epoch(runtime, dependencies)
    if result is None:
        raise RuntimeError
    return result
