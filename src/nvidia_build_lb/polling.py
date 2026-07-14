"""Single-deadline same-key NVIDIA 202 polling and terminal acquisition."""

from contextlib import suppress

import anyio
from pydantic import SecretStr

from nvidia_build_lb.headers import (
    HeaderProtocolError,
    ValidatedUpstreamHeaders,
    validate_upstream_headers,
)
from nvidia_build_lb.nvidia_types import (
    NvidiaResponse,
    NvidiaTransportError,
    OwnedNvidiaResponse,
)
from nvidia_build_lb.outcomes import (
    PollDeadline,
    TransportSignal,
    map_public_outcome,
)
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.poll_response_retirement import (
    close_response_with_deadline,
    retire_response_preserving_primary,
)
from nvidia_build_lb.poll_terminal_resolution import (
    protocol_failure as _protocol_failure,
)
from nvidia_build_lb.poll_terminal_resolution import (
    resolve_terminal_response,
)
from nvidia_build_lb.poll_terminal_types import FailureTerminal, JsonTerminal
from nvidia_build_lb.polling_types import (
    JitterSource,
    MonotonicClock,
    NvidiaSSEStream,
    PollDeadlineExpiredError,
    PollDependencies,
    PollingDeadline,
    PollTerminal,
    Sleeper,
    StreamTerminal,
)
from nvidia_build_lb.request_wire import NvidiaWireRequest, build_nvidia_request
from nvidia_build_lb.response_retirement import (
    ResponseRetirementUnresolvedError,
    retire_response,
)
from nvidia_build_lb.transport_common import PinnedTransportDriftError

__all__ = [
    "FailureTerminal",
    "JitterSource",
    "JsonTerminal",
    "MonotonicClock",
    "NvidiaSSEStream",
    "PollDeadlineExpiredError",
    "PollDependencies",
    "PollTerminal",
    "PollingDeadline",
    "ResponseRetirementUnresolvedError",
    "Sleeper",
    "StreamTerminal",
    "close_response_with_deadline",
    "poll_nvcf_result",
    "resolve_terminal_response",
    "retire_response",
    "retire_response_preserving_primary",
]

_POLL_CAPS = (0.25, 0.5, 1.0, 2.0)
_ASYNC_STATUS = 202


async def poll_nvcf_result(
    *,
    dependencies: PollDependencies,
    credential: SecretStr,
    request_id: str,
    deadline: PollingDeadline,
) -> PollTerminal:
    """Use equal jitter for every poll while retaining one key and deadline."""
    poll_delay_index = 0
    active_responses: list[NvidiaResponse] = []
    try:
        while True:
            await _sleep_before_poll(dependencies, deadline, poll_delay_index)
            request = build_nvidia_request(
                credential=credential,
                body=None,
                origin_request_id=request_id,
            )

            async def send_and_own(
                remaining: float,
                selected_request: NvidiaWireRequest = request,
            ) -> NvidiaResponse:
                owned = OwnedNvidiaResponse(
                    await dependencies.client.send(selected_request, timeout_seconds=remaining)
                )
                active_responses.append(owned)
                return owned

            response = await deadline.run(send_and_own)
            headers, retirement_unresolved = await _validated_headers(
                response,
                dependencies,
                request_id,
                deadline,
            )
            if headers is None:
                active_responses.clear()
                return _protocol_failure(retirement_unresolved)
            if response.status_code == _ASYNC_STATUS:
                await close_response_with_deadline(response, deadline)
                active_responses.clear()
                poll_delay_index += 1
                continue
            terminal = await resolve_terminal_response(
                response,
                headers,
                deadline,
                dependencies.fail_stop,
            )
            active_responses.clear()
            if isinstance(terminal, FailureTerminal):
                return FailureTerminal(
                    outcome=terminal.outcome.without_alternate(),
                    retry_after_seconds=terminal.retry_after_seconds,
                    retirement_unresolved=terminal.retirement_unresolved,
                )
            return terminal
    except PollDeadlineExpiredError:
        retirement_unresolved = await _retire_latest_preserving_primary(active_responses)
        return FailureTerminal(
            outcome=map_public_outcome(PollDeadline()),
            retry_after_seconds=None,
            retirement_unresolved=retirement_unresolved,
        )
    except NvidiaTransportError as error:
        retirement_unresolved = await _retire_latest_preserving_primary(active_responses)
        mapped = map_public_outcome(TransportSignal(error.code, error.request_bytes_sent))
        return FailureTerminal(
            outcome=mapped.without_alternate(),
            retry_after_seconds=None,
            retirement_unresolved=retirement_unresolved,
        )
    except BaseException as error:
        await _retire_after_primary(active_responses, error)
        raise


async def _validated_headers(
    response: NvidiaResponse,
    dependencies: PollDependencies,
    request_id: str,
    deadline: PollingDeadline,
) -> tuple[ValidatedUpstreamHeaders | None, bool]:
    headers: ValidatedUpstreamHeaders | None = None
    with suppress(HeaderProtocolError):
        headers = validate_upstream_headers(
            status_code=response.status_code,
            raw_headers=response.raw_headers,
            now=dependencies.wall_clock.now(),
            origin_request_id=request_id,
        )
    if headers is None:
        unresolved = await retire_response_preserving_primary(response, deadline)
        return None, unresolved
    return headers, False


async def _sleep_before_poll(
    dependencies: PollDependencies,
    deadline: PollingDeadline,
    poll_delay_index: int,
) -> None:
    delay = _equal_jitter_delay(dependencies.jitter, poll_delay_index)
    await deadline.run(lambda remaining: dependencies.sleeper.sleep(min(delay, remaining)))


async def _retire_latest_preserving_primary(active_responses: list[NvidiaResponse]) -> bool:
    if active_responses:
        return await retire_response_preserving_primary(active_responses.pop())
    return False


async def _retire_after_primary(
    active_responses: list[NvidiaResponse],
    primary: BaseException,
) -> None:
    """Retire polling state without stealing routing's drift fail-stop ordering."""
    if isinstance(primary, ResponseRetirementUnresolvedError):
        # This response already consumed its one bounded close attempt. Routing
        # owns terminal persistence and fail-stop; never reopen cleanup here.
        active_responses.clear()
        return
    if isinstance(primary, (PinnedRuntimeDriftError, PinnedTransportDriftError)):
        # The routing coordinator must persist CANCELLED before it withdraws
        # readiness and cancels the process root. A second failure while closing
        # the active poll response is cleanup evidence, not an earlier owner of
        # fail-stop ordering.
        with suppress(BaseException):
            _ = await _retire_latest_preserving_primary(active_responses)
        return
    # Routing owns every pre-handoff fatal transition. Cancellation needs an
    # explicit fatal signal only when physical response retirement is unknown;
    # ordinary exceptions already enter routing's generic fatal path.
    unresolved = await _retire_latest_preserving_primary(active_responses)
    if unresolved and isinstance(primary, anyio.get_cancelled_exc_class()):
        raise ResponseRetirementUnresolvedError from None


def _equal_jitter_delay(jitter: JitterSource, exponent: int) -> float:
    cap = _POLL_CAPS[min(max(exponent, 0), len(_POLL_CAPS) - 1)]
    return jitter.uniform(cap / 2, cap)
