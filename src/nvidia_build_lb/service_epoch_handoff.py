"""Bounded one-packet service disposition handoff over AF_UNIX."""

import json
import socket
from dataclasses import dataclass
from functools import partial
from typing import ClassVar, Final
from uuid import UUID

from anyio.to_thread import run_sync
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from nvidia_build_lb.service_epoch_cleanup import (
    ExitDisposition,
    ExitKind,
    HandoffResult,
)
from nvidia_build_lb.strict_json import load_unique_json

_MAX_PACKET_BYTES: Final = 4096
_ACK: Final = b"ACK"
_NACK: Final = b"NACK"


class DispositionProtocolError(ValueError):
    """The local cleanup packet violated the closed protocol."""


class _DispositionDocument(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True, strict=True)

    cleanup_causes: list[str]
    epoch_id: str
    exit_status: int
    kind: str
    mode: str
    nonce: str
    primary_reason: str | None


_DOCUMENT_ADAPTER: Final[TypeAdapter[_DispositionDocument]] = TypeAdapter(_DispositionDocument)


@dataclass(frozen=True, slots=True)
class ReceivedDisposition:
    """One accepted disposition or one closed rejection reason."""

    disposition: ExitDisposition | None
    rejection: str | None


@dataclass(frozen=True, slots=True)
class SeqpacketDispositionSender:
    """Send exactly one disposition and wait for one bounded ACK."""

    channel: socket.socket
    timeout_seconds: float = 1.0

    async def send(self, disposition: ExitDisposition) -> HandoffResult:
        """Map socket send and ACK outcomes to the closed handoff enum."""
        packet = encode_disposition(disposition)
        try:
            sent = await run_sync(partial(_send_packet, self.channel, packet, self.timeout_seconds))
        except (OSError, TimeoutError):
            return HandoffResult.SEND_TIMEOUT
        if sent != len(packet):
            return HandoffResult.SEND_TIMEOUT
        try:
            reply = await run_sync(partial(_receive_packet, self.channel, self.timeout_seconds))
        except (OSError, TimeoutError):
            return HandoffResult.ACK_TIMEOUT
        except ValueError:
            return HandoffResult.NACK
        return HandoffResult.ACK if reply == _ACK else HandoffResult.NACK


@dataclass(frozen=True, slots=True)
class SeqpacketDispositionReceiver:
    """Receive, validate, and ACK one exact child disposition."""

    channel: socket.socket
    timeout_seconds: float = 1.0

    async def receive(self, *, epoch_id: UUID, nonce: UUID) -> ReceivedDisposition:
        """Reject malformed, mismatched, truncated, or duplicate-key packets."""
        try:
            packet = await run_sync(partial(_receive_packet, self.channel, self.timeout_seconds))
        except (OSError, TimeoutError):
            return ReceivedDisposition(None, "missing_disposition")
        except ValueError:
            _ = await self._reply(_NACK)
            return ReceivedDisposition(None, "rejected_disposition")
        try:
            disposition = decode_disposition(packet)
        except (UnicodeDecodeError, ValueError):
            _ = await self._reply(_NACK)
            return ReceivedDisposition(None, "rejected_disposition")
        if disposition.epoch_id != epoch_id or disposition.nonce != nonce:
            _ = await self._reply(_NACK)
            return ReceivedDisposition(None, "rejected_disposition")
        if not await self._reply(_ACK):
            return ReceivedDisposition(None, "ack_timeout")
        return ReceivedDisposition(disposition, None)

    async def _reply(self, reply: bytes) -> bool:
        try:
            sent = await run_sync(partial(_send_packet, self.channel, reply, self.timeout_seconds))
        except (OSError, TimeoutError):
            return False
        return sent == len(reply)


def disposition_socketpair() -> tuple[socket.socket, socket.socket]:
    """Create one close-on-exec Linux AF_UNIX SOCK_SEQPACKET pair."""
    return socket.socketpair(
        socket.AF_UNIX,
        socket.SOCK_SEQPACKET | socket.SOCK_CLOEXEC,
    )


def encode_disposition(disposition: ExitDisposition) -> bytes:
    """Encode canonical secret-free UTF-8 JSON within one packet."""
    document = {
        "cleanup_causes": list(disposition.cleanup_causes),
        "epoch_id": str(disposition.epoch_id),
        "exit_status": disposition.exit_status,
        "kind": disposition.kind.value,
        "mode": disposition.mode,
        "nonce": str(disposition.nonce),
        "primary_reason": disposition.primary_reason,
    }
    packet = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(packet) > _MAX_PACKET_BYTES:
        raise DispositionProtocolError
    return packet


def decode_disposition(packet: bytes) -> ExitDisposition:
    """Decode one strict exact-field disposition without lossy coercion."""
    if not packet or len(packet) > _MAX_PACKET_BYTES:
        raise DispositionProtocolError
    try:
        parsed = load_unique_json(packet.decode("utf-8", errors="strict"))
    except (TypeError, UnicodeDecodeError, ValueError):
        raise DispositionProtocolError from None
    try:
        document = _DOCUMENT_ADAPTER.validate_python(parsed)
    except ValidationError:
        raise DispositionProtocolError from None
    if document.exit_status < 0:
        raise DispositionProtocolError
    epoch_id = _canonical_uuid(document.epoch_id)
    nonce = _canonical_uuid(document.nonce)
    try:
        kind = ExitKind(document.kind)
    except ValueError:
        raise DispositionProtocolError from None
    disposition = ExitDisposition(
        kind=kind,
        mode=document.mode,
        primary_reason=document.primary_reason,
        cleanup_causes=tuple(document.cleanup_causes),
        exit_status=document.exit_status,
        epoch_id=epoch_id,
        nonce=nonce,
    )
    if not _valid_disposition(disposition):
        raise DispositionProtocolError
    return disposition


def _canonical_uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError:
        raise DispositionProtocolError from None
    if str(parsed) != value:
        raise DispositionProtocolError
    return parsed


def _valid_disposition(disposition: ExitDisposition) -> bool:
    if disposition.kind is ExitKind.STOPPED:
        return (
            disposition.mode == "clean"
            and disposition.primary_reason is None
            and not disposition.cleanup_causes
            and disposition.exit_status == 0
        )
    if disposition.mode != "clean_cleanup_failed":
        return False
    allowed_causes = {"unlock_false", "unlock_exception", "close_exception", "late_fatal"}
    causes = set(disposition.cleanup_causes)
    if len(causes) != len(disposition.cleanup_causes) or not causes <= allowed_causes:
        return False
    if {"unlock_false", "unlock_exception"} <= causes:
        return False
    canonical_causes = tuple(
        cause
        for cause in ("unlock_exception", "unlock_false", "close_exception", "late_fatal")
        if cause in causes
    )
    if disposition.cleanup_causes != canonical_causes:
        return False
    expected_reason = next(
        (
            cause
            for cause in ("late_fatal", "close_exception", "unlock_exception", "unlock_false")
            if cause in causes
        ),
        None,
    )
    expected_status = {
        "unlock_false": 72,
        "unlock_exception": 73,
        "close_exception": 74,
        "late_fatal": 79,
    }.get(expected_reason or "")
    return (
        expected_reason is not None
        and disposition.primary_reason == expected_reason
        and disposition.exit_status == expected_status
    )


def _send_packet(channel: socket.socket, packet: bytes, timeout_seconds: float) -> int:
    channel.settimeout(timeout_seconds)
    return channel.send(packet)


def _receive_packet(channel: socket.socket, timeout_seconds: float) -> bytes:
    channel.settimeout(timeout_seconds)
    received = channel.recvmsg(_MAX_PACKET_BYTES)
    packet = received[0]
    flags = received[2]
    if flags & socket.MSG_TRUNC:
        raise ValueError
    return packet
