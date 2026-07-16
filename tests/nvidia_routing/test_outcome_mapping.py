"""Closed source-signal to safe public-outcome mappings."""

import pytest

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.outcomes import (
    DownstreamLoss,
    HttpStatusSignal,
    LedgerCapacityExhausted,
    NoEligibleKey,
    PollDeadline,
    ProtocolFailure,
    PublicOutcome,
    ReservationFailure,
    RoutingTransition,
    SourceSignal,
    TransportErrorCode,
    TransportSignal,
    map_public_outcome,
)

pytestmark = pytest.mark.nvidia_routing


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        (
            NoEligibleKey(),
            PublicOutcome(
                http_status=503,
                code="no_upstream_keys",
                message="no upstream keys available",
                persisted_status=None,
                alternate_eligible=False,
            ),
        ),
        (
            ReservationFailure(),
            PublicOutcome(
                http_status=503,
                code="database_unavailable",
                message="database unavailable",
                persisted_status=None,
                alternate_eligible=False,
            ),
        ),
        (
            LedgerCapacityExhausted(),
            PublicOutcome(
                http_status=503,
                code="ledger_capacity_exhausted",
                message="request evidence capacity exhausted",
                persisted_status=None,
                alternate_eligible=False,
            ),
        ),
        (
            HttpStatusSignal(401),
            PublicOutcome(
                http_status=502,
                code="upstream_auth_error",
                message="upstream authentication failed",
                persisted_status=LastStatusClass.INVALID_CREDENTIAL,
                alternate_eligible=True,
                transition=RoutingTransition.QUARANTINE,
            ),
        ),
        (
            HttpStatusSignal(402),
            PublicOutcome(
                http_status=503,
                code="upstream_credits_exhausted",
                message="upstream credits exhausted",
                persisted_status=LastStatusClass.CREDITS_EXHAUSTED,
                alternate_eligible=True,
                transition=RoutingTransition.QUARANTINE,
            ),
        ),
        (
            HttpStatusSignal(408),
            PublicOutcome(
                http_status=504,
                code="upstream_timeout",
                message="upstream request timed out",
                persisted_status=LastStatusClass.TIMEOUT,
                alternate_eligible=True,
                transition=RoutingTransition.TRANSIENT_COOLDOWN,
            ),
        ),
        (
            HttpStatusSignal(429),
            PublicOutcome(
                http_status=429,
                code="upstream_rate_limited",
                message="upstream rate limited",
                persisted_status=LastStatusClass.RATE_LIMITED,
                alternate_eligible=True,
                transition=RoutingTransition.RATE_COOLDOWN,
            ),
        ),
        (
            HttpStatusSignal(422),
            PublicOutcome(
                http_status=422,
                code="upstream_request_rejected",
                message="upstream request rejected",
                persisted_status=LastStatusClass.REQUEST_REJECTED,
                alternate_eligible=False,
            ),
        ),
        (
            HttpStatusSignal(500),
            PublicOutcome(
                http_status=502,
                code="upstream_internal_error",
                message="upstream internal error",
                persisted_status=LastStatusClass.UPSTREAM_INTERNAL_ERROR,
                alternate_eligible=False,
            ),
        ),
        (
            HttpStatusSignal(502),
            PublicOutcome(
                http_status=502,
                code="upstream_bad_gateway",
                message="upstream bad gateway",
                persisted_status=LastStatusClass.UPSTREAM_BAD_GATEWAY,
                alternate_eligible=True,
                transition=RoutingTransition.TRANSIENT_COOLDOWN,
            ),
        ),
        (
            HttpStatusSignal(503),
            PublicOutcome(
                http_status=503,
                code="upstream_unavailable",
                message="upstream unavailable",
                persisted_status=LastStatusClass.UPSTREAM_UNAVAILABLE,
                alternate_eligible=True,
                transition=RoutingTransition.TRANSIENT_COOLDOWN,
            ),
        ),
        (
            HttpStatusSignal(418),
            PublicOutcome(
                http_status=418,
                code="upstream_request_rejected",
                message="upstream request rejected",
                persisted_status=LastStatusClass.REQUEST_REJECTED,
                alternate_eligible=False,
            ),
        ),
        (
            HttpStatusSignal(451),
            PublicOutcome(
                http_status=451,
                code="upstream_request_rejected",
                message="upstream request rejected",
                persisted_status=LastStatusClass.REQUEST_REJECTED,
                alternate_eligible=False,
            ),
        ),
        (
            ProtocolFailure(),
            PublicOutcome(
                http_status=502,
                code="upstream_protocol_error",
                message="upstream protocol error",
                persisted_status=LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
                alternate_eligible=False,
                transition=RoutingTransition.DEGRADE_HEALTH_ONLY,
            ),
        ),
    ],
)
def test_http_and_local_outcomes_are_exact(signal: SourceSignal, expected: PublicOutcome) -> None:
    assert map_public_outcome(signal) == expected


@pytest.mark.parametrize(
    ("signal", "expected_status", "alternate"),
    [
        (
            TransportSignal(TransportErrorCode.CONNECT_TIMEOUT, request_bytes_sent=0),
            LastStatusClass.TIMEOUT,
            True,
        ),
        (
            TransportSignal(TransportErrorCode.CONNECT_ERROR, request_bytes_sent=0),
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            True,
        ),
        (
            TransportSignal(TransportErrorCode.POOL_TIMEOUT, request_bytes_sent=0),
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            False,
        ),
        (
            TransportSignal(TransportErrorCode.READ_TIMEOUT, request_bytes_sent=1),
            LastStatusClass.TIMEOUT,
            False,
        ),
        (
            TransportSignal(TransportErrorCode.READ_ERROR, request_bytes_sent=1),
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            False,
        ),
    ],
)
def test_transport_alternate_is_only_safe_before_send(
    signal: TransportSignal,
    expected_status: LastStatusClass | None,
    alternate: bool,
) -> None:
    mapped = map_public_outcome(signal)

    assert mapped.persisted_status is expected_status
    assert mapped.alternate_eligible is alternate


def test_poll_deadline_and_downstream_loss_are_distinct_terminal_classes() -> None:
    deadline = map_public_outcome(PollDeadline())
    downstream = map_public_outcome(DownstreamLoss(delivery_exception=False))
    delivery = map_public_outcome(DownstreamLoss(delivery_exception=True))

    assert deadline.code == "poll_timeout"
    assert deadline.persisted_status is LastStatusClass.TIMEOUT
    assert downstream.http_status is None
    assert downstream.persisted_status is LastStatusClass.CANCELLED
    assert delivery.persisted_status is LastStatusClass.DELIVERY_FAILED


def test_signal_repr_never_contains_provider_error_text() -> None:
    signal = TransportSignal(TransportErrorCode.READ_ERROR, request_bytes_sent=8)

    assert "provider" not in repr(signal)
