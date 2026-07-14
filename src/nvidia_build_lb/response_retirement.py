"""Hard-bounded physical retirement for one owned NVIDIA response."""

from typing import override

import anyio

from nvidia_build_lb.async_cleanup import AsyncCleanupUnresolvedError, run_bounded_cleanup
from nvidia_build_lb.nvidia_types import NvidiaResponse
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.transport_common import PinnedTransportDriftError

_RESPONSE_RETIRE_TIMEOUT_SECONDS = 1.0


class ResponseRetirementUnresolvedError(Exception):
    """The process must fail-stop because physical response retirement is unknown."""

    @override
    def __str__(self) -> str:
        return "response_retirement_unresolved"


async def retire_response(response: NvidiaResponse) -> None:
    """Bound physical retirement while shielding it from request cancellation."""
    unresolved = False
    try:
        with anyio.CancelScope(shield=True):
            _ = await run_bounded_cleanup(
                response.aclose,
                _RESPONSE_RETIRE_TIMEOUT_SECONDS,
            )
    except (
        AsyncCleanupUnresolvedError,
        PinnedRuntimeDriftError,
        PinnedTransportDriftError,
    ):
        unresolved = True
    if unresolved:
        raise ResponseRetirementUnresolvedError from None
