"""Closed source signals and safe public outcome types."""

from dataclasses import dataclass
from enum import StrEnum, unique
from typing import Self

from nvidia_build_lb.admin.schemas import LastStatusClass


@unique
class RoutingTransition(StrEnum):
    """Route-state effects selected independently from serialization."""

    PRESERVE = "preserve"
    QUARANTINE = "quarantine"
    RATE_COOLDOWN = "rate_cooldown"
    TRANSIENT_COOLDOWN = "transient_cooldown"
    DEGRADE_HEALTH_ONLY = "degrade_health_only"


@unique
class TransportErrorCode(StrEnum):
    """Safe transport errors that never retain exception text."""

    CONNECT_TIMEOUT = "connect_timeout"
    CONNECT_ERROR = "connect_error"
    POOL_TIMEOUT = "pool_timeout"
    READ_TIMEOUT = "read_timeout"
    WRITE_TIMEOUT = "write_timeout"
    READ_ERROR = "read_error"
    WRITE_ERROR = "write_error"
    UNSUPPORTED_PROTOCOL = "unsupported_protocol"
    LOCAL_PROTOCOL_ERROR = "local_protocol_error"
    REMOTE_PROTOCOL_ERROR = "remote_protocol_error"
    PROTOCOL_ERROR = "protocol_error"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"


@dataclass(frozen=True, slots=True)
class NoEligibleKey:
    """No recoverable key was eligible for the first reservation."""


@dataclass(frozen=True, slots=True)
class ReservationFailure:
    """Local durable reservation was not proven committed or absent."""


@dataclass(frozen=True, slots=True)
class LedgerCapacityExhausted:
    """Local exact-evidence ledger cannot admit another routed attempt."""


@dataclass(frozen=True, slots=True)
class HttpStatusSignal:
    """An upstream status observed before downstream response start."""

    status_code: int


@dataclass(frozen=True, slots=True)
class ProtocolFailure:
    """A provider response violated the closed wire or representation contract."""


@dataclass(frozen=True, slots=True)
class TransportSignal:
    """A safe transport class plus whether request bytes may have left."""

    error: TransportErrorCode
    request_bytes_sent: int


@dataclass(frozen=True, slots=True)
class PollDeadline:
    """The single monotonic 202 polling deadline expired."""


@dataclass(frozen=True, slots=True)
class DownstreamLoss:
    """Downstream was lost before terminal CAS or payload delivery."""

    delivery_exception: bool


type SourceSignal = (
    NoEligibleKey
    | ReservationFailure
    | LedgerCapacityExhausted
    | HttpStatusSignal
    | ProtocolFailure
    | TransportSignal
    | PollDeadline
    | DownstreamLoss
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PublicOutcome:
    """Safe response and persistence decision for one observation."""

    http_status: int | None
    code: str | None
    message: str | None
    persisted_status: LastStatusClass | None
    alternate_eligible: bool
    transition: RoutingTransition = RoutingTransition.PRESERVE

    def without_alternate(self) -> Self:
        """Retain the result while forbidding a post-202 key change."""
        return type(self)(
            http_status=self.http_status,
            code=self.code,
            message=self.message,
            persisted_status=self.persisted_status,
            alternate_eligible=False,
            transition=self.transition,
        )
