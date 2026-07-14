"""Acquire one strict terminal representation from an owned NVIDIA response."""

from nvidia_build_lb.attempt_fail_stop import AttemptFailStop
from nvidia_build_lb.headers import MediaType, ValidatedUpstreamHeaders
from nvidia_build_lb.nvidia_types import NvidiaResponse
from nvidia_build_lb.outcomes import HttpStatusSignal, ProtocolFailure, map_public_outcome
from nvidia_build_lb.poll_response_retirement import (
    retire_response_preserving_primary,
)
from nvidia_build_lb.poll_terminal_types import FailureTerminal, JsonTerminal
from nvidia_build_lb.polling_types import (
    NvidiaSSEStream,
    PollingDeadline,
    PollTerminal,
    StreamTerminal,
)
from nvidia_build_lb.representations import (
    MAX_JSON_REPRESENTATION_BYTES,
    BoundedResponseReader,
    RepresentationProtocolError,
    read_json_representation,
)
from nvidia_build_lb.sse import SSEProtocolError

_SUCCESS_STATUS = 200


async def resolve_terminal_response(
    response: NvidiaResponse,
    headers: ValidatedUpstreamHeaders,
    deadline: PollingDeadline,
    fail_stop: AttemptFailStop,
) -> PollTerminal:
    """Close unread errors or acquire one strict JSON/SSE terminal representation."""
    if response.status_code != _SUCCESS_STATUS:
        unresolved = await retire_response_preserving_primary(response, deadline)
        return FailureTerminal(
            map_public_outcome(HttpStatusSignal(response.status_code)),
            headers.retry_after_seconds,
            retirement_unresolved=unresolved,
        )
    if headers.media_type is MediaType.JSON:
        return await _resolve_json(response, headers, deadline)
    if headers.media_type is MediaType.SSE:
        return await _resolve_sse(response, headers, deadline, fail_stop)
    unresolved = await retire_response_preserving_primary(response, deadline)
    return protocol_failure(unresolved)


async def _resolve_json(
    response: NvidiaResponse,
    headers: ValidatedUpstreamHeaders,
    deadline: PollingDeadline,
) -> JsonTerminal | FailureTerminal:
    reader = BoundedResponseReader(
        max_bytes=MAX_JSON_REPRESENTATION_BYTES,
        expected_content_length=headers.content_length,
    )
    try:
        raw = await deadline.run(lambda _remaining: reader.read(response))
        representation = read_json_representation(raw)
    except RepresentationProtocolError:
        unresolved = await retire_response_preserving_primary(response, deadline)
        return protocol_failure(unresolved)
    unresolved = await retire_response_preserving_primary(response, deadline)
    return JsonTerminal(representation, headers, retirement_unresolved=unresolved)


async def _resolve_sse(
    response: NvidiaResponse,
    headers: ValidatedUpstreamHeaders,
    deadline: PollingDeadline,
    fail_stop: AttemptFailStop,
) -> StreamTerminal | FailureTerminal:
    try:
        stream = await NvidiaSSEStream.prime(response, headers, deadline, fail_stop)
    except (RepresentationProtocolError, SSEProtocolError):
        unresolved = await retire_response_preserving_primary(response, deadline)
        return protocol_failure(unresolved)
    return StreamTerminal(stream, headers)


def protocol_failure(retirement_unresolved: bool = False) -> FailureTerminal:
    """Build the safe provider-protocol terminal with optional fatal cleanup state."""
    return FailureTerminal(
        map_public_outcome(ProtocolFailure()),
        None,
        retirement_unresolved=retirement_unresolved,
    )
