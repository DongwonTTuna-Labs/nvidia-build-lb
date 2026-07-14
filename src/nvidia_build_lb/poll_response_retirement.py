"""Deadline-aware response retirement for NVIDIA polling."""

import anyio

from nvidia_build_lb.async_cleanup import AsyncCleanupUnresolvedError, run_bounded_cleanup
from nvidia_build_lb.nvidia_types import NvidiaResponse
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.polling_types import PollDeadlineExpiredError, PollingDeadline
from nvidia_build_lb.response_retirement import (
    ResponseRetirementUnresolvedError,
    retire_response,
)
from nvidia_build_lb.transport_common import PinnedTransportDriftError


async def close_response_with_deadline(
    response: NvidiaResponse,
    deadline: PollingDeadline,
) -> None:
    """Close without shielding so the absolute origin deadline remains effective."""
    try:
        _ = await run_bounded_cleanup(response.aclose, deadline.remaining())
    except (
        AsyncCleanupUnresolvedError,
        PinnedRuntimeDriftError,
        PinnedTransportDriftError,
        anyio.get_cancelled_exc_class(),
    ):
        raise ResponseRetirementUnresolvedError from None
    _ = deadline.remaining()


async def retire_response_preserving_primary(
    response: NvidiaResponse,
    deadline: PollingDeadline | None = None,
) -> bool:
    """Return whether selected-terminal cleanup left retirement unresolved."""
    try:
        if deadline is None:
            await retire_response(response)
        else:
            await close_response_with_deadline(response, deadline)
    except PollDeadlineExpiredError:
        return await _retire_after_expired_deadline(response)
    except ResponseRetirementUnresolvedError:
        return True
    except anyio.get_cancelled_exc_class():
        return True
    except Exception:  # noqa: BLE001 - ordinary close failure cannot replace a safe terminal.
        return False
    return False


async def _retire_after_expired_deadline(response: NvidiaResponse) -> bool:
    """Use the fixed cleanup budget when the terminal deadline cannot own close."""
    try:
        await retire_response(response)
    except ResponseRetirementUnresolvedError:
        return True
    except anyio.get_cancelled_exc_class():
        return True
    except Exception:  # noqa: BLE001 - an ordinary close failure cannot replace the terminal.
        return False
    return False
