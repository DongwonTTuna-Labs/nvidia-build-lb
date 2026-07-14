"""Safe live-stream terminal proposals for typed transport failures."""

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.nvidia_types import NvidiaTransportError
from nvidia_build_lb.outcomes import TransportSignal, map_public_outcome
from nvidia_build_lb.terminal import TerminalProposal


def transport_failure_proposal(
    error: NvidiaTransportError,
    *,
    sequence: int,
    request_id: str,
) -> TerminalProposal:
    """Preserve the safe transport class while forbidding mid-stream replay."""
    outcome = map_public_outcome(
        TransportSignal(error.code, error.request_bytes_sent)
    ).without_alternate()
    return TerminalProposal.stream_failure(
        sequence=sequence,
        status_class=outcome.persisted_status or LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
        request_id=request_id,
    )
