"""Typed wire helpers shared by the intentional-red contracts."""

from dataclasses import dataclass
from typing import Annotated, ClassVar, Protocol

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue

ACCEPTED_HOST = "127.0.0.1:2456"
ACCEPTED_ORIGIN = "http://127.0.0.1:2456"
ADMIN_TOKEN = f"nblb_admin_{'a' * 64}"
MODELS_TOKEN = f"nblb_ds_{'b' * 64}"
CHAT_TOKEN = f"nblb_ds_{'c' * 64}"
REVOKED_TOKEN = f"nblb_ds_{'d' * 64}"
UPSTREAM_KEY = "nvapi-contract-plaintext-sentinel"
ENABLED_KEY_ID = "00000000-0000-4000-8000-000000000001"
DISABLED_KEY_ID = "00000000-0000-4000-8000-000000000002"
DOWNSTREAM_ID = "00000000-0000-4000-8000-000000000003"

CSP = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'"
)


class _WireErrorDetail(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: Annotated[str, Field(min_length=1)]


class _WireErrorEnvelope(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    error: _WireErrorDetail


@dataclass(frozen=True, slots=True)
class ContractSeed:
    """Synthetic identities future dependency overrides must bind."""

    admin_token: str = ADMIN_TOKEN
    models_token: str = MODELS_TOKEN
    chat_token: str = CHAT_TOKEN
    revoked_token: str = REVOKED_TOKEN
    enabled_key_id: str = ENABLED_KEY_ID
    disabled_key_id: str = DISABLED_KEY_ID
    downstream_id: str = DOWNSTREAM_ID


@dataclass(frozen=True, slots=True)
class ContractClient:
    """A TestClient that defaults every non-boundary request to accepted edges."""

    app: FastAPI
    http: TestClient
    readiness: "ReadinessControl"

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: JsonValue = None,
    ) -> Response:
        request_headers = {"Host": ACCEPTED_HOST, "Origin": ACCEPTED_ORIGIN}
        if headers is not None:
            request_headers.update(headers)
        return self.http.request(method, path, headers=request_headers, json=json_body)

    def set_ready(self, ready: bool) -> None:
        """Set only the injected deterministic readiness seam."""
        self.readiness.set_ready(ready)


class ReadinessControl(Protocol):
    """Mutable contract-only readiness control."""

    def set_ready(self, ready: bool) -> None: ...


def bearer(token: str) -> dict[str, str]:
    """Build one synthetic bearer header without persisting it."""
    return {"Authorization": f"Bearer {token}"}


def assert_error(
    response: Response,
    *,
    status_code: int,
    code: str,
    realm: str | None = None,
) -> None:
    """Assert the closed safe error wire shape and optional bearer realm."""
    assert response.status_code == status_code
    assert response.headers.get("content-type") == "application/json"
    assert response.content.startswith(b'{"error":')
    envelope = _WireErrorEnvelope.model_validate_json(response.content)
    assert envelope.error.code == code
    if realm is not None:
        assert response.headers["www-authenticate"] == f'Bearer realm="{realm}"'
    assert_no_cors(response)


def assert_security_headers(response: Response) -> None:
    """Assert the exact browser/admin response policies."""
    assert response.headers.get("cache-control") == "no-store"
    assert response.headers.get("referrer-policy") == "no-referrer"
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("content-security-policy") == CSP
    assert_no_cors(response)


def assert_no_cors(response: Response) -> None:
    """Reject every CORS grant header regardless of casing."""
    assert not any(name.lower().startswith(b"access-control-") for name, _ in response.headers.raw)


def assert_excludes(response: Response, *forbidden: str) -> None:
    """Assert response headers and body contain no supplied sensitive marker."""
    serialized_headers = "\n".join(f"{name}: {value}" for name, value in response.headers.items())
    corpus = f"{serialized_headers}\n{response.text}".lower()
    for marker in forbidden:
        assert marker.lower() not in corpus
