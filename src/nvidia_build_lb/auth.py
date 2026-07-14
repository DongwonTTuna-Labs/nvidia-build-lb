"""Realm-separated constant-time bearer authentication."""

import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Final

from fastapi import Request, Response
from pydantic import SecretStr
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import ASGIApp, Receive, Scope, Send

from nvidia_build_lb.admin.schemas import DownstreamScope
from nvidia_build_lb.admin_route_shapes import requires_admin_authentication
from nvidia_build_lb.credential_protocols import DownstreamCredentialRepository
from nvidia_build_lb.credential_types import (
    AuthenticationRejectedError,
    AuthRealm,
    DownstreamPrincipal,
    InsufficientScopeError,
)
from nvidia_build_lb.request_id import request_id_from
from nvidia_build_lb.schemas import ErrorEnvelope

_BEARER_PREFIX: Final = b"Bearer "
_ADMIN_PREFIX: Final = b"nblb_admin_"
_DOWNSTREAM_PREFIX: Final = b"nblb_ds_"
_HEX_BYTES: Final = 64
_LOWER_HEX: Final = frozenset(b"0123456789abcdef")


def _parse_token(headers: Sequence[bytes], prefix: bytes) -> bytes | None:
    if len(headers) != 1:
        return None
    raw = headers[0]
    if b"," in raw or not raw.startswith(_BEARER_PREFIX):
        return None
    token = raw.removeprefix(_BEARER_PREFIX)
    suffix = token.removeprefix(prefix)
    if (
        not token.startswith(prefix)
        or len(suffix) != _HEX_BYTES
        or any(byte not in _LOWER_HEX for byte in suffix)
    ):
        return None
    return token


@dataclass(frozen=True, slots=True)
class AdminAuthenticator:
    """Authenticate only one exact singleton admin bearer header."""

    expected_token: SecretStr

    def authenticate(self, authorization_headers: Sequence[bytes]) -> None:
        """Compare a shape-valid admin bearer with constant-time equality."""
        candidate = _parse_token(authorization_headers, _ADMIN_PREFIX)
        expected = self.expected_token.get_secret_value().encode()
        comparable = b"\x00" * len(expected) if candidate is None else candidate
        matches = hmac.compare_digest(comparable, expected)
        if candidate is None or not matches:
            raise AuthenticationRejectedError(realm=AuthRealm.ADMIN)


@dataclass(frozen=True, slots=True)
class DownstreamAuthenticator:
    """Parse the downstream realm then delegate digest/scope persistence."""

    repository: DownstreamCredentialRepository

    async def authenticate(
        self,
        authorization_headers: Sequence[bytes],
        required_scope: DownstreamScope,
    ) -> DownstreamPrincipal:
        """Return a principal only after its authorized-use commit succeeds."""
        candidate = _parse_token(authorization_headers, _DOWNSTREAM_PREFIX)
        if candidate is None:
            raise AuthenticationRejectedError(realm=AuthRealm.DOWNSTREAM)
        return await self.repository.authorize_digest(sha256(candidate).digest(), required_scope)


@dataclass(frozen=True, slots=True)
class CredentialAuthenticators:
    """Bundle the two deliberately separate bearer realms."""

    admin: AdminAuthenticator
    downstream: DownstreamAuthenticator


@dataclass(frozen=True, slots=True)
class _AuthError:
    status_code: int
    code: str
    message: str
    realm: AuthRealm | None = None


_AUTH_ERRORS: Final = MappingProxyType(
    {
        AuthRealm.ADMIN: _AuthError(
            401,
            "admin_unauthorized",
            "admin authentication required",
            AuthRealm.ADMIN,
        ),
        AuthRealm.DOWNSTREAM: _AuthError(
            401,
            "unauthorized",
            "authentication required",
            AuthRealm.DOWNSTREAM,
        ),
    }
)


def _error_response(error: _AuthError, request_id: str) -> Response:
    headers = (
        {} if error.realm is None else {"WWW-Authenticate": f'Bearer realm="{error.realm.value}"'}
    )
    return Response(
        status_code=error.status_code,
        content=ErrorEnvelope.from_safe_parts(
            error.code,
            error.message,
            request_id,
        ).model_dump_json(),
        media_type="application/json",
        headers=headers,
    )


def _authorization_headers(request: Request) -> tuple[bytes, ...]:
    return tuple(value for name, value in request.headers.raw if name.lower() == b"authorization")


class CredentialAuthMiddleware:
    """Authenticate exact admin/public routes before route dispatch."""

    _app: ASGIApp
    _authenticators: CredentialAuthenticators

    def __init__(self, app: ASGIApp, authenticators: CredentialAuthenticators) -> None:
        """Bind the exact realm authenticators to this middleware instance."""
        self._app = app
        self._authenticators = authenticators

    async def _authenticate(self, request: Request) -> Response | None:
        headers = _authorization_headers(request)
        path = request.url.path
        request_id = request_id_from(request)
        try:
            if requires_admin_authentication(request.method, path):
                self._authenticators.admin.authenticate(headers)
            elif request.method == "GET" and path == "/v1/models":
                _ = await self._authenticators.downstream.authenticate(
                    headers,
                    DownstreamScope.MODELS_READ,
                )
            elif request.method == "POST" and path == "/v1/chat/completions":
                _ = await self._authenticators.downstream.authenticate(
                    headers,
                    DownstreamScope.CHAT_WRITE,
                )
        except AuthenticationRejectedError as error:
            return _error_response(_AUTH_ERRORS[error.realm], request_id)
        except InsufficientScopeError:
            return _error_response(
                _AuthError(403, "insufficient_scope", "required scope is missing"),
                request_id,
            )
        except (OSError, SQLAlchemyError):
            return _error_response(
                _AuthError(503, "database_unavailable", "database unavailable"),
                request_id,
            )
        else:
            return None

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """Return an auth failure or dispatch only after successful authorization."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        rejected = await self._authenticate(request)
        if rejected is not None:
            await rejected(scope, receive, send)
            return
        await self._app(scope, receive, send)
