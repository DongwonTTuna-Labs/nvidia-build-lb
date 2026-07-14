"""Fully acquired non-stream NVIDIA terminal values."""

from dataclasses import dataclass

from nvidia_build_lb.headers import ValidatedUpstreamHeaders
from nvidia_build_lb.outcomes import PublicOutcome
from nvidia_build_lb.representations import JsonRepresentation


@dataclass(frozen=True, slots=True)
class JsonTerminal:
    """A fully consumed and closed strict JSON terminal response."""

    representation: JsonRepresentation
    headers: ValidatedUpstreamHeaders
    retirement_unresolved: bool = False


@dataclass(frozen=True, slots=True)
class FailureTerminal:
    """A safe failure mapping whose response body was never consumed."""

    outcome: PublicOutcome
    retry_after_seconds: int | None
    retirement_unresolved: bool = False
