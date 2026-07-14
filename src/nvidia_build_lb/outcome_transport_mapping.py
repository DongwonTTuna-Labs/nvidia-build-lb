"""Closed transport-error mapping with safe pre-send alternate rules."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.outcome_disposition import (
    DEGRADE,
    PRESERVE,
    TRANSIENT_ALTERNATE,
    Disposition,
)
from nvidia_build_lb.outcome_types import PublicOutcome, TransportErrorCode


def _public(
    http_status: int,
    code: str,
    message: str,
    persisted_status: LastStatusClass,
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


_PROTOCOL_OUTCOME: Final = _public(
    502,
    "upstream_protocol_error",
    "upstream protocol error",
    LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
    DEGRADE,
)
_TRANSPORT_OUTCOMES: Final[Mapping[TransportErrorCode, PublicOutcome]] = MappingProxyType(
    {
        TransportErrorCode.CONNECT_TIMEOUT: _public(
            504, "upstream_timeout", "upstream request timed out", LastStatusClass.TIMEOUT
        ),
        TransportErrorCode.CONNECT_ERROR: _public(
            503,
            "upstream_unavailable",
            "upstream unavailable",
            LastStatusClass.UPSTREAM_UNAVAILABLE,
        ),
        TransportErrorCode.POOL_TIMEOUT: _public(
            503,
            "upstream_unavailable",
            "upstream unavailable",
            LastStatusClass.UPSTREAM_UNAVAILABLE,
        ),
        TransportErrorCode.READ_TIMEOUT: _public(
            504, "upstream_timeout", "upstream request timed out", LastStatusClass.TIMEOUT
        ),
        TransportErrorCode.WRITE_TIMEOUT: _public(
            504, "upstream_timeout", "upstream request timed out", LastStatusClass.TIMEOUT
        ),
        TransportErrorCode.TIMEOUT: _public(
            504, "upstream_timeout", "upstream request timed out", LastStatusClass.TIMEOUT
        ),
        TransportErrorCode.READ_ERROR: _public(
            503,
            "upstream_unavailable",
            "upstream unavailable",
            LastStatusClass.UPSTREAM_UNAVAILABLE,
        ),
        TransportErrorCode.WRITE_ERROR: _public(
            503,
            "upstream_unavailable",
            "upstream unavailable",
            LastStatusClass.UPSTREAM_UNAVAILABLE,
        ),
        TransportErrorCode.NETWORK_ERROR: _public(
            503,
            "upstream_unavailable",
            "upstream unavailable",
            LastStatusClass.UPSTREAM_UNAVAILABLE,
        ),
        TransportErrorCode.UNSUPPORTED_PROTOCOL: _PROTOCOL_OUTCOME,
        TransportErrorCode.LOCAL_PROTOCOL_ERROR: _PROTOCOL_OUTCOME,
        TransportErrorCode.REMOTE_PROTOCOL_ERROR: _PROTOCOL_OUTCOME,
        TransportErrorCode.PROTOCOL_ERROR: _PROTOCOL_OUTCOME,
    }
)


def map_transport(error: TransportErrorCode, request_bytes_sent: int) -> PublicOutcome:
    """Allow an alternate only for a proven zero-byte connect failure."""
    if error is TransportErrorCode.CONNECT_TIMEOUT and request_bytes_sent == 0:
        return _public(
            504,
            "upstream_timeout",
            "upstream request timed out",
            LastStatusClass.TIMEOUT,
            TRANSIENT_ALTERNATE,
        )
    if error is TransportErrorCode.CONNECT_ERROR and request_bytes_sent == 0:
        return _public(
            503,
            "upstream_unavailable",
            "upstream unavailable",
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            TRANSIENT_ALTERNATE,
        )
    return _TRANSPORT_OUTCOMES[error]
