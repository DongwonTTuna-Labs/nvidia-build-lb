"""Deterministic aggregation precedence for sequential routing failures."""

from collections.abc import Mapping
from typing import Final

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.outcomes import NoEligibleKey, ProtocolFailure, map_public_outcome
from nvidia_build_lb.polling import FailureTerminal

_STATUS_PRECEDENCE: Final[Mapping[LastStatusClass, int]] = {
    LastStatusClass.RATE_LIMITED: 0,
    LastStatusClass.TIMEOUT: 1,
    LastStatusClass.UPSTREAM_UNAVAILABLE: 2,
    LastStatusClass.UPSTREAM_BAD_GATEWAY: 3,
    LastStatusClass.INVALID_CREDENTIAL: 4,
    LastStatusClass.CREDITS_EXHAUSTED: 5,
}


def aggregate_failures(failures: list[FailureTerminal]) -> FailureTerminal:
    """Select the fixed public failure without depending on completion order."""
    if not failures:
        return FailureTerminal(
            outcome=map_public_outcome(NoEligibleKey()),
            retry_after_seconds=None,
        )
    return min(failures, key=_failure_precedence)


def is_aggregate_member(failure: FailureTerminal) -> bool:
    """Return whether a sequential retry failure participates in precedence."""
    if failure.outcome.code == "poll_timeout":
        return False
    status = failure.outcome.persisted_status
    return status in _STATUS_PRECEDENCE


def protocol_failure() -> FailureTerminal:
    """Build the one provider-representation protocol failure."""
    return FailureTerminal(
        outcome=map_public_outcome(ProtocolFailure()),
        retry_after_seconds=None,
    )


def _failure_precedence(failure: FailureTerminal) -> tuple[int, int]:
    status = failure.outcome.persisted_status
    rank = 6 if status is None else _STATUS_PRECEDENCE.get(status, 6)
    retry_after = failure.retry_after_seconds
    return rank, 2**31 - 1 if retry_after is None else retry_after
