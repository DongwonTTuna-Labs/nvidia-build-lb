"""Immutable allowlisted NVIDIA origin and 202 poll requests."""

from dataclasses import dataclass, field
from enum import StrEnum, unique
from typing import ClassVar, Literal, Self, override

from pydantic import SecretStr

from nvidia_build_lb.headers import is_valid_nvcf_request_id

_WIRE_ERROR = "nvidia_request_wire_error"


class RequestWireError(Exception):
    """Reject a request that cannot satisfy the fixed wire contract."""

    @override
    def __str__(self) -> str:
        """Return a safe fixed code."""
        return _WIRE_ERROR


@unique
class NvidiaAuthority(StrEnum):
    """Closed fixed authority selector for origin POST and poll GET."""

    ORIGIN = "origin"
    POLL = "poll"


@dataclass(frozen=True, slots=True)
class NvidiaWireRequest:
    """One immutable request whose sensitive fields are excluded from repr."""

    ORIGIN_PATH: ClassVar[str] = "/v1/chat/completions"
    POLL_PREFIX: ClassVar[str] = "/v2/nvcf/pexec/status/"

    authority: NvidiaAuthority
    method: Literal["GET", "POST"]
    path: str
    credential: SecretStr = field(repr=False)
    body: bytes | None = field(repr=False)

    def __post_init__(self) -> None:
        """Reject every mismatched authority, method, path, body, or credential."""
        _validate_credential(self.credential)
        request_id = self.path.removeprefix(self.POLL_PREFIX)
        valid = (
            self.authority is NvidiaAuthority.ORIGIN
            and self.method == "POST"
            and self.path == self.ORIGIN_PATH
            and bool(self.body)
        ) or (
            self.authority is NvidiaAuthority.POLL
            and self.method == "GET"
            and self.path.startswith(self.POLL_PREFIX)
            and is_valid_nvcf_request_id(request_id)
            and self.body is None
        )
        if not valid:
            raise RequestWireError

    @classmethod
    def origin(cls, *, credential: SecretStr, body: bytes) -> Self:
        """Build one POST using already validated serialized bytes."""
        return cls(
            authority=NvidiaAuthority.ORIGIN,
            method="POST",
            path=cls.ORIGIN_PATH,
            credential=credential,
            body=body,
        )

    @classmethod
    def poll(
        cls,
        *,
        credential: SecretStr,
        origin_request_id: str,
        body: bytes | None = None,
    ) -> Self:
        """Build one body-free GET pinned to the origin key and request ID."""
        return cls(
            authority=NvidiaAuthority.POLL,
            method="GET",
            path=f"{cls.POLL_PREFIX}{origin_request_id}",
            credential=credential,
            body=body,
        )

    @property
    def application_header_names(self) -> tuple[str, ...]:
        """Return the exact application-owned header names without values."""
        if self.method == "POST":
            return ("Authorization", "Content-Type", "Accept", "Accept-Encoding")
        return ("Authorization", "Accept", "Accept-Encoding")

    def application_headers(self) -> tuple[tuple[bytes, bytes], ...]:
        """Materialize allowlisted wire bytes only at the adapter boundary."""
        authorization = b"Bearer " + self.credential.get_secret_value().encode("utf-8")
        common = (
            (b"Accept", b"application/json, text/event-stream"),
            (b"Accept-Encoding", b"identity"),
        )
        if self.method == "POST":
            return (
                (b"Authorization", authorization),
                (b"Content-Type", b"application/json"),
                *common,
            )
        return ((b"Authorization", authorization), *common)


def build_nvidia_request(
    *,
    credential: SecretStr,
    body: bytes | None,
    origin_request_id: str | None = None,
) -> NvidiaWireRequest:
    """Select the fixed origin or poll constructor without alternate encoders."""
    if origin_request_id is None:
        if body is None:
            raise RequestWireError
        return NvidiaWireRequest.origin(credential=credential, body=body)
    return NvidiaWireRequest.poll(
        credential=credential,
        origin_request_id=origin_request_id,
        body=body,
    )


def _validate_credential(credential: SecretStr) -> None:
    value = credential.get_secret_value()
    if not value or "\r" in value or "\n" in value or "\x00" in value:
        raise RequestWireError
    try:
        _ = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        utf8_valid = False
    else:
        utf8_valid = True
    if not utf8_valid:
        raise RequestWireError
