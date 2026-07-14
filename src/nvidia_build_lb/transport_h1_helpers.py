"""Validation and bounded raw-stream cleanup for the pinned H1 transport."""

from collections.abc import Awaitable, Callable
from typing import Final

import anyio
from pydantic import ConfigDict, TypeAdapter, ValidationError

from nvidia_build_lb.async_cleanup import AsyncCleanupUnresolvedError, run_bounded_cleanup
from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.transport_common import PinnedTransportDriftError
from nvidia_build_lb.transport_core import CoreAsyncStream, TransportCore, mapped_error

_RAW_STREAM_RETIRE_TIMEOUT_SECONDS: Final = 1.0
_TIMEOUT_EXTENSION_ADAPTER: TypeAdapter[dict[str, dict[str, float]]] = TypeAdapter(
    dict[str, dict[str, float]],
    config=ConfigDict(strict=True),
)


async def bounded_transport_cleanup(
    operation: Callable[[], Awaitable[None]],
    timeout_seconds: float,
    *,
    sanitize_exception: bool = False,
) -> BaseException | None:
    """Return one safe cleanup failure after a hard same-loop observation budget."""
    try:
        with anyio.CancelScope(shield=True):
            _ = await run_bounded_cleanup(operation, timeout_seconds)
    except AsyncCleanupUnresolvedError:
        return PinnedTransportDriftError()
    except BaseException as error:  # noqa: BLE001 - caller preserves only a safe primary.
        if sanitize_exception and isinstance(error, Exception):
            return PinnedTransportDriftError()
        return error
    return None


def timeout_extension(extensions: object) -> dict[str, object]:
    """Accept only the exact four-value timeout extension."""
    try:
        validated = _TIMEOUT_EXTENSION_ADAPTER.validate_python(extensions)
    except ValidationError:
        raise PinnedTransportDriftError from None
    if set(validated) != {"timeout"}:
        raise PinnedTransportDriftError
    timeout = validated["timeout"]
    if set(timeout) != {"connect", "pool", "read", "write"}:
        raise PinnedTransportDriftError
    return {"timeout": timeout}


async def retire_raw_stream(
    stream: CoreAsyncStream,
    request: httpx2.Request,
    core: TransportCore,
) -> None:
    """Bound construction-failure retirement and expose only safe exceptions."""
    failure: Exception | None = None
    try:
        with anyio.CancelScope(shield=True):
            _ = await run_bounded_cleanup(
                stream.aclose,
                _RAW_STREAM_RETIRE_TIMEOUT_SECONDS,
            )
    except AsyncCleanupUnresolvedError:
        failure = PinnedTransportDriftError()
    except Exception as error:  # noqa: BLE001 - closed core exception surface.
        failure = mapped_error(error, request, core)
    if failure is not None:
        raise failure
