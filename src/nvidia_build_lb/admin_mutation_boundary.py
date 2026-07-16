"""Authenticated pre-body mutation deadline, barrier, and response buffer."""

from dataclasses import dataclass, field
from typing import Final, Protocol

import anyio
from fastapi import Request, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nvidia_build_lb.admin_deadlines import (
    AdminMutationResponseInvalidError,
    AdminMutationTimeoutError,
    RuntimeUnavailableError,
)
from nvidia_build_lb.admin_http_errors import safe_credential_error
from nvidia_build_lb.admin_mutation_barrier import AdminMutationBarrier
from nvidia_build_lb.admin_route_shapes import is_admin_mutation
from nvidia_build_lb.request_id import request_id_from

_MAX_RESPONSE_BYTES: Final = 65_536


class LifecycleAvailability(Protocol):
    """Read the process-local lifecycle bit without database I/O."""

    def is_ready(self) -> bool:
        """Return current publication/readiness state."""
        ...


@dataclass(frozen=True, slots=True)
class AlwaysOperationalLifecycle:
    """Explicit isolated-test lifecycle seam."""

    def is_ready(self) -> bool:
        """Keep isolated credential tests operational."""
        return True


@dataclass(slots=True)
class _ResponseBuffer:
    start: Message | None = field(default=None, init=False)
    chunks: list[bytes] = field(default_factory=list, init=False)
    size: int = field(default=0, init=False)
    complete: bool = field(default=False, init=False)

    async def send(self, message: Message) -> None:
        """Capture one strict non-streaming response without forwarding it."""
        message_type: object = message.get("type")
        if message_type == "http.response.start":
            if self.start is not None or self.complete:
                raise AdminMutationResponseInvalidError
            self.start = dict(message)
            return
        if message_type != "http.response.body" or self.start is None or self.complete:
            raise AdminMutationResponseInvalidError
        body_value: object = message.get("body")
        more_body_value: object = message.get("more_body")
        if body_value is None and "body" not in message:
            body_value = b""
        if more_body_value is None and "more_body" not in message:
            more_body_value = False
        if not isinstance(body_value, bytes) or not isinstance(more_body_value, bool):
            raise AdminMutationResponseInvalidError
        body = body_value
        more_body = more_body_value
        self.size += len(body)
        if self.size > _MAX_RESPONSE_BYTES:
            raise AdminMutationResponseInvalidError
        self.chunks.append(body)
        self.complete = not more_body

    async def flush(self, send: Send) -> None:
        """Publish exactly one start and one complete body after validation."""
        if self.start is None or not self.complete:
            raise AdminMutationResponseInvalidError
        await send(self.start)
        await send(
            {
                "type": "http.response.body",
                "body": b"".join(self.chunks),
                "more_body": False,
            }
        )


class AdminMutationBoundaryMiddleware:
    """Own every authenticated mutation from pre-body admission through buffer."""

    _app: ASGIApp
    _barrier: AdminMutationBarrier
    _lifecycle: LifecycleAvailability
    _deadline_seconds: int

    def __init__(
        self,
        app: ASGIApp,
        *,
        barrier: AdminMutationBarrier,
        lifecycle: LifecycleAvailability,
        deadline_seconds: int,
    ) -> None:
        """Bind one shared settlement barrier and lifecycle gate."""
        self._app = app
        self._barrier = barrier
        self._lifecycle = lifecycle
        self._deadline_seconds = deadline_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bound only exact admin mutations; pass every other ASGI call through."""
        scope_type: object = scope.get("type")
        method: object = scope.get("method")
        path: object = scope.get("path")
        if (
            scope_type != "http"
            or not isinstance(method, str)
            or not isinstance(path, str)
            or not is_admin_mutation(method, path)
        ):
            await self._app(scope, receive, send)
            return
        request_id = request_id_from(Request(scope, receive=receive))
        self._barrier.enter()
        buffered = _ResponseBuffer()
        error: Exception | None = None
        try:
            if not self._lifecycle.is_ready():
                error = RuntimeUnavailableError()
            else:
                try:
                    with anyio.fail_after(self._deadline_seconds):
                        await self._app(scope, receive, buffered.send)
                    if not buffered.complete:
                        error = AdminMutationResponseInvalidError()
                except TimeoutError:
                    error = AdminMutationTimeoutError()
                except AdminMutationResponseInvalidError as invalid:
                    error = invalid
        finally:
            self._barrier.exit()
        if error is None:
            await buffered.flush(send)
            return
        await _error_response(error, request_id)(scope, receive, send)


def _error_response(error: Exception, request_id: str) -> Response:
    if isinstance(error, RuntimeUnavailableError):
        return safe_credential_error(
            503,
            "runtime_unavailable",
            "runtime unavailable",
            request_id,
        )
    if isinstance(error, AdminMutationTimeoutError):
        return safe_credential_error(
            504,
            "admin_mutation_timeout",
            "mutation outcome unknown",
            request_id,
        )
    return safe_credential_error(
        502,
        "admin_mutation_response_invalid",
        "mutation outcome unknown",
        request_id,
    )
