"""Closed upstream observations and their safe public mappings."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, assert_never

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.outcome_disposition import (
    DEGRADE,
    PRESERVE,
    QUARANTINE,
    RATE_ALTERNATE,
    TRANSIENT_ALTERNATE,
    TRANSIENT_TERMINAL,
    Disposition,
)
from nvidia_build_lb.outcome_transport_mapping import map_transport
from nvidia_build_lb.outcome_types import (
    DownstreamLoss,
    HttpStatusSignal,
    NoEligibleKey,
    PollDeadline,
    ProtocolFailure,
    PublicOutcome,
    ReservationFailure,
    RoutingTransition,
    SourceSignal,
    TransportErrorCode,
    TransportSignal,
)

__all__ = [
    "DownstreamLoss",
    "HttpStatusSignal",
    "NoEligibleKey",
    "PollDeadline",
    "ProtocolFailure",
    "PublicOutcome",
    "ReservationFailure",
    "RoutingTransition",
    "SourceSignal",
    "TransportErrorCode",
    "TransportSignal",
    "map_public_outcome",
]


def _public(
    http_status: int | None,
    code: str | None,
    message: str | None,
    persisted_status: LastStatusClass | None = None,
    disposition: Disposition = PRESERVE,
) -> PublicOutcome:
    return PublicOutcome(
        http_status=http_status,
        code=code,
        message=message,
        persisted_status=persisted_status,
        alternate_eligible=disposition.alternate,
        transition=disposition.transition,
    )


_HTTP_STATUS_OUTCOMES: Final[Mapping[int, PublicOutcome]] = MappingProxyType(
    {
        401: _public(
            502,
            "upstream_auth_error",
            "upstream authentication failed",
            LastStatusClass.INVALID_CREDENTIAL,
            QUARANTINE,
        ),
        403: _public(
            502,
            "upstream_auth_error",
            "upstream authentication failed",
            LastStatusClass.INVALID_CREDENTIAL,
            QUARANTINE,
        ),
        402: _public(
            503,
            "upstream_credits_exhausted",
            "upstream credits exhausted",
            LastStatusClass.CREDITS_EXHAUSTED,
            QUARANTINE,
        ),
        408: _public(
            504,
            "upstream_timeout",
            "upstream request timed out",
            LastStatusClass.TIMEOUT,
            TRANSIENT_ALTERNATE,
        ),
        504: _public(
            504,
            "upstream_timeout",
            "upstream request timed out",
            LastStatusClass.TIMEOUT,
            TRANSIENT_ALTERNATE,
        ),
        429: _public(
            429,
            "upstream_rate_limited",
            "upstream rate limited",
            LastStatusClass.RATE_LIMITED,
            RATE_ALTERNATE,
        ),
        500: _public(
            502,
            "upstream_internal_error",
            "upstream internal error",
            LastStatusClass.UPSTREAM_INTERNAL_ERROR,
        ),
        502: _public(
            502,
            "upstream_bad_gateway",
            "upstream bad gateway",
            LastStatusClass.UPSTREAM_BAD_GATEWAY,
            TRANSIENT_ALTERNATE,
        ),
        503: _public(
            503,
            "upstream_unavailable",
            "upstream unavailable",
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            TRANSIENT_ALTERNATE,
        ),
    }
)
_PROTOCOL_OUTCOME: Final = _public(
    502,
    "upstream_protocol_error",
    "upstream protocol error",
    LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
    DEGRADE,
)
_MIN_REQUEST_REJECTION_STATUS: Final = 400
_MAX_REQUEST_REJECTION_STATUS: Final = 499


def map_public_outcome(signal: SourceSignal) -> PublicOutcome:
    """Map one closed signal without reading provider-controlled text."""
    match signal:
        case NoEligibleKey():
            outcome = _public(503, "no_upstream_keys", "no upstream keys available")
        case ReservationFailure():
            outcome = _public(503, "database_unavailable", "database unavailable")
        case HttpStatusSignal(status_code=status_code):
            outcome = _map_http_status(status_code)
        case ProtocolFailure():
            outcome = _PROTOCOL_OUTCOME
        case TransportSignal(error=error, request_bytes_sent=request_bytes_sent):
            outcome = map_transport(error, request_bytes_sent)
        case PollDeadline():
            outcome = _public(
                504,
                "poll_timeout",
                "upstream polling timed out",
                LastStatusClass.TIMEOUT,
                TRANSIENT_TERMINAL,
            )
        case DownstreamLoss(delivery_exception=delivery_exception):
            status = (
                LastStatusClass.DELIVERY_FAILED if delivery_exception else LastStatusClass.CANCELLED
            )
            outcome = _public(None, None, None, status)
        case _:
            assert_never(signal)
    return outcome


def _map_http_status(status_code: int) -> PublicOutcome:
    special = _HTTP_STATUS_OUTCOMES.get(status_code)
    if special is not None:
        return special
    if _MIN_REQUEST_REJECTION_STATUS <= status_code <= _MAX_REQUEST_REJECTION_STATUS:
        return _public(
            status_code,
            "upstream_request_rejected",
            "upstream request rejected",
            LastStatusClass.REQUEST_REJECTED,
        )
    return _PROTOCOL_OUTCOME
