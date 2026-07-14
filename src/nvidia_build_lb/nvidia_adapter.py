"""NVIDIA hosted origin POST and same-key terminal response adapter."""

from contextlib import suppress
from dataclasses import dataclass

import anyio
from pydantic import SecretStr

from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.credential_types import Clock
from nvidia_build_lb.headers import (
    HeaderProtocolError,
    ValidatedUpstreamHeaders,
    validate_upstream_headers,
)
from nvidia_build_lb.nvidia_types import (
    NvidiaClient,
    NvidiaResponse,
    NvidiaTransportError,
    OwnedNvidiaResponse,
)
from nvidia_build_lb.outcomes import (
    PollDeadline,
    ProtocolFailure,
    TransportErrorCode,
    TransportSignal,
    map_public_outcome,
)
from nvidia_build_lb.pinned_runtime import PinnedRuntimeDriftError
from nvidia_build_lb.polling import (
    FailureTerminal,
    JitterSource,
    MonotonicClock,
    PollDeadlineExpiredError,
    PollDependencies,
    PollingDeadline,
    PollTerminal,
    ResponseRetirementUnresolvedError,
    Sleeper,
    close_response_with_deadline,
    poll_nvcf_result,
    resolve_terminal_response,
    retire_response,
    retire_response_preserving_primary,
)
from nvidia_build_lb.request_wire import build_nvidia_request
from nvidia_build_lb.transport_common import PinnedTransportDriftError

_ORIGIN_TIMEOUT_SECONDS = 120.0
_POLL_DEADLINE_SECONDS = 60.0
_ASYNC_STATUS = 202


@dataclass(frozen=True, slots=True)
class NvidiaAdapterDependencies:
    """Shared client, clocks, sleep, and jitter dependencies."""

    client: NvidiaClient
    monotonic_clock: MonotonicClock
    wall_clock: Clock
    sleeper: Sleeper
    jitter: JitterSource
    fail_stop: AttemptFailStop


@dataclass(frozen=True, slots=True)
class NvidiaHostedAdapter:
    """Execute one NVIDIA origin call and fully own any 202 polling chain."""

    dependencies: NvidiaAdapterDependencies

    async def execute(self, *, credential: SecretStr, body: bytes) -> PollTerminal:
        """POST once, then acquire a terminal JSON/SSE/error representation."""
        request = build_nvidia_request(credential=credential, body=body)
        try:
            response = OwnedNvidiaResponse(
                await self.dependencies.client.send(
                    request,
                    timeout_seconds=_ORIGIN_TIMEOUT_SECONDS,
                )
            )
        except NvidiaTransportError as error:
            return FailureTerminal(
                outcome=map_public_outcome(TransportSignal(error.code, error.request_bytes_sent)),
                retry_after_seconds=None,
            )
        try:
            return await _execute_owned(self.dependencies, response, credential)
        except (PinnedRuntimeDriftError, PinnedTransportDriftError):
            # Routing owns the durable CANCELLED terminal and process fail-stop for
            # drift. Cleanup may itself observe the same drift, but it must never
            # trigger fail-stop before that terminal is committed.
            with suppress(BaseException):
                await retire_response(response)
            raise
        except ResponseRetirementUnresolvedError:
            # The routing coordinator owns the reserved attempt. It must commit
            # CANCELLED before withdrawing readiness and cancelling the root.
            raise
        except BaseException as primary:
            try:
                await retire_response(response)
            except ResponseRetirementUnresolvedError:
                if isinstance(primary, anyio.get_cancelled_exc_class()):
                    raise
            except BaseException as error:  # noqa: BLE001 - preserve the request primary.
                del error
            raise


async def _execute_owned(
    dependencies: NvidiaAdapterDependencies,
    response: NvidiaResponse,
    credential: SecretStr,
) -> PollTerminal:
    """Acquire a terminal representation while the caller retains cleanup ownership."""
    headers: ValidatedUpstreamHeaders | None = None
    with suppress(HeaderProtocolError):
        headers = validate_upstream_headers(
            status_code=response.status_code,
            raw_headers=response.raw_headers,
            now=dependencies.wall_clock.now(),
        )
    if headers is None:
        unresolved = await retire_response_preserving_primary(response)
        return _protocol_failure(unresolved)

    if response.status_code == _ASYNC_STATUS:
        request_id = headers.request_id
        if request_id is None:
            unresolved = await retire_response_preserving_primary(response)
            return _protocol_failure(unresolved)
        deadline = PollingDeadline(
            clock=dependencies.monotonic_clock,
            expires_at=dependencies.monotonic_clock.monotonic() + _POLL_DEADLINE_SECONDS,
        )
        try:
            await close_response_with_deadline(response, deadline)
        except PollDeadlineExpiredError:
            unresolved = await retire_response_preserving_primary(response)
            return FailureTerminal(
                outcome=map_public_outcome(PollDeadline()),
                retry_after_seconds=None,
                retirement_unresolved=unresolved,
            )
        except NvidiaTransportError as error:
            unresolved = await retire_response_preserving_primary(response)
            return FailureTerminal(
                outcome=map_public_outcome(
                    TransportSignal(error.code, error.request_bytes_sent)
                ).without_alternate(),
                retry_after_seconds=None,
                retirement_unresolved=unresolved,
            )
        return await poll_nvcf_result(
            dependencies=PollDependencies(
                client=dependencies.client,
                monotonic_clock=dependencies.monotonic_clock,
                wall_clock=dependencies.wall_clock,
                sleeper=dependencies.sleeper,
                jitter=dependencies.jitter,
                fail_stop=dependencies.fail_stop,
            ),
            credential=credential,
            request_id=request_id,
            deadline=deadline,
        )

    terminal_deadline = PollingDeadline(
        clock=dependencies.monotonic_clock,
        expires_at=dependencies.monotonic_clock.monotonic() + _ORIGIN_TIMEOUT_SECONDS,
    )
    return await _resolve_direct_terminal(
        response,
        headers,
        terminal_deadline,
        dependencies.fail_stop,
    )


def _protocol_failure(retirement_unresolved: bool = False) -> FailureTerminal:
    return FailureTerminal(
        outcome=map_public_outcome(ProtocolFailure()),
        retry_after_seconds=None,
        retirement_unresolved=retirement_unresolved,
    )


async def _resolve_direct_terminal(
    response: NvidiaResponse,
    headers: ValidatedUpstreamHeaders,
    deadline: PollingDeadline,
    fail_stop: AttemptFailStop,
) -> PollTerminal:
    try:
        return await resolve_terminal_response(response, headers, deadline, fail_stop)
    except PollDeadlineExpiredError:
        unresolved = await retire_response_preserving_primary(response)
        return FailureTerminal(
            outcome=map_public_outcome(
                TransportSignal(TransportErrorCode.READ_TIMEOUT, 1)
            ).without_alternate(),
            retry_after_seconds=None,
            retirement_unresolved=unresolved,
        )
    except NvidiaTransportError as error:
        unresolved = await retire_response_preserving_primary(response)
        return FailureTerminal(
            outcome=map_public_outcome(
                TransportSignal(error.code, error.request_bytes_sent)
            ).without_alternate(),
            retry_after_seconds=None,
            retirement_unresolved=unresolved,
        )
