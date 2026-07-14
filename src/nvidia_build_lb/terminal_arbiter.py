"""Deterministic terminal proposal ordering and per-frame release types."""

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, unique
from typing import Final

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.attempt_types import CooldownKind
from nvidia_build_lb.scheduler_state import TerminalOutcome


@unique
class TerminalKind(StrEnum):
    """Priority-ordered closed terminal discriminators."""

    DEADLINE_EXPIRED = "DeadlineExpired"
    EXPLICIT_DISCONNECT = "ExplicitDisconnect"
    ANCESTOR_CANCELLED = "AncestorCancelled"
    SEND_CANCELLED = "SendCancelled"
    SEND_OS_ERROR = "SendOSError"
    SEND_EXCEPTION = "SendException"
    NETWORK_TERMINAL_FAILURE = "NetworkTerminalFailure"
    NETWORK_TERMINAL_SUCCESS = "NetworkTerminalSuccess"


_PRIORITY: Final = {kind: index for index, kind in enumerate(TerminalKind)}
_STREAM_ERROR_CODE: Final = "upstream_stream_error"
_STREAM_ERROR_MESSAGE: Final = "upstream stream ended unexpectedly"


@dataclass(frozen=True, slots=True)
class TerminalProposal:
    """One typed terminal candidate whose payload is never printable."""

    kind: TerminalKind
    sequence: int
    outcome: TerminalOutcome
    status_class: LastStatusClass
    payload_kind: str
    payload: bytes | None = field(default=None, repr=False)
    cooldown_until: datetime | None = None
    cooldown_kind: CooldownKind | None = None
    reraise: bool = False

    @classmethod
    def for_kind(
        cls,
        kind: TerminalKind,
        *,
        sequence: int,
        request_id: str = "opaque",
    ) -> "TerminalProposal":
        """Build the canonical no-provider-data candidate for a discriminator."""
        if kind is TerminalKind.DEADLINE_EXPIRED:
            return cls(kind, sequence, TerminalOutcome.FAILED, LastStatusClass.TIMEOUT, "none")
        if kind in {
            TerminalKind.EXPLICIT_DISCONNECT,
            TerminalKind.ANCESTOR_CANCELLED,
            TerminalKind.SEND_CANCELLED,
            TerminalKind.SEND_OS_ERROR,
        }:
            return cls(
                kind,
                sequence,
                TerminalOutcome.CANCELLED,
                LastStatusClass.CANCELLED,
                "none",
                reraise=kind in {TerminalKind.ANCESTOR_CANCELLED, TerminalKind.SEND_CANCELLED},
            )
        if kind is TerminalKind.SEND_EXCEPTION:
            return cls(
                kind,
                sequence,
                TerminalOutcome.FAILED,
                LastStatusClass.DELIVERY_FAILED,
                "none",
                reraise=True,
            )
        if kind is TerminalKind.NETWORK_TERMINAL_FAILURE:
            return cls.stream_failure(
                sequence=sequence,
                status_class=LastStatusClass.UPSTREAM_PROTOCOL_ERROR,
                request_id=request_id,
            )
        return cls.network_success(sequence=sequence, payload=b"data: [DONE]\n\n")

    @classmethod
    def network_failure(
        cls,
        *,
        sequence: int,
        status_class: LastStatusClass,
        payload: bytes,
        cooldown_until: datetime | None = None,
        cooldown_kind: CooldownKind | None = None,
    ) -> "TerminalProposal":
        """Build one immutable canonical local-error candidate."""
        return cls(
            TerminalKind.NETWORK_TERMINAL_FAILURE,
            sequence,
            TerminalOutcome.FAILED,
            status_class,
            "canonical_local_error",
            payload,
            cooldown_until,
            cooldown_kind,
        )

    @classmethod
    def stream_failure(
        cls,
        *,
        sequence: int,
        status_class: LastStatusClass,
        request_id: str,
    ) -> "TerminalProposal":
        """Build the one safe request-correlated mid-stream wire terminal."""
        payload = (
            b"event: error\ndata: "
            + json.dumps(
                {
                    "error": {
                        "code": _STREAM_ERROR_CODE,
                        "message": _STREAM_ERROR_MESSAGE,
                        "request_id": request_id,
                    }
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n\n"
        )
        return cls.network_failure(
            sequence=sequence,
            status_class=status_class,
            payload=payload,
        )

    @classmethod
    def network_success(cls, *, sequence: int, payload: bytes) -> "TerminalProposal":
        """Build one immutable original-DONE success candidate."""
        return cls(
            TerminalKind.NETWORK_TERMINAL_SUCCESS,
            sequence,
            TerminalOutcome.SUCCEEDED,
            LastStatusClass.SUCCESS,
            "original_DONE",
            payload,
        )

    def canonical_tuple(self) -> tuple[str, str, str, str, str]:
        """Return the byte-content-independent deterministic duplicate key."""
        digest = "" if self.payload is None else hashlib.sha256(self.payload).hexdigest()
        return (
            self.kind.value,
            f"{self.sequence:020d}",
            self.status_class.value,
            self.payload_kind,
            digest,
        )


@dataclass(frozen=True, slots=True)
class TerminalDecision:
    """One winning candidate and whether this call owned the only CAS."""

    winner: TerminalProposal
    cas_success: bool
    discarded_count: int


@unique
class FrameReleaseKind(StrEnum):
    """One immutable per-generation network-child release decision."""

    CONTINUE = "Continue"
    STOP = "Stop"


@dataclass(frozen=True, slots=True)
class ProposalAcceptance:
    """FIFO acceptance receipt, including late next-generation routing."""

    requested_generation: int
    accepted_generation: int
    acceptance_sequence: int


@dataclass(frozen=True, slots=True)
class FrameReleaseDecision:
    """The one release linearization result for a frame generation."""

    generation: int
    kind: FrameReleaseKind
    acceptance_cutoff: int
    terminal: TerminalDecision | None


class TerminalArbiter:
    """Select fixed priority from a closed batch and latch exactly once."""

    _lock: threading.Lock
    _winner: TerminalProposal | None

    def __init__(self) -> None:
        """Initialize one unlatched generation."""
        self._lock = threading.Lock()
        self._winner = None

    def register_one(self, proposal: TerminalProposal) -> TerminalDecision:
        """Register one candidate through the same batch path."""
        return self.register_batch((proposal,))

    def register_batch(self, proposals: tuple[TerminalProposal, ...]) -> TerminalDecision:
        """Collapse duplicates, apply priority, and perform one process-local CAS."""
        if not proposals:
            raise ValueError
        by_kind: dict[TerminalKind, TerminalProposal] = {}
        for proposal in proposals:
            current = by_kind.get(proposal.kind)
            if current is None or proposal.canonical_tuple() < current.canonical_tuple():
                by_kind[proposal.kind] = proposal
        candidate = min(by_kind.values(), key=lambda proposal: _PRIORITY[proposal.kind])
        with self._lock:
            if self._winner is not None:
                return TerminalDecision(
                    winner=self._winner,
                    cas_success=False,
                    discarded_count=len(proposals),
                )
            self._winner = candidate
        return TerminalDecision(
            winner=candidate,
            cas_success=True,
            discarded_count=len(proposals) - 1,
        )
