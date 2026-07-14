import hashlib

import anyio
import pytest

from nvidia_build_lb.admin.schemas import (
    AdminEventListResponse,
    AdminOverviewRead,
    AdminValidationErrorResponse,
    DownstreamScope,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
)
from nvidia_build_lb.schemas import ErrorEnvelope

from .conftest import ADMIN_TOKEN, CredentialHttpSlice

pytestmark = pytest.mark.vault_auth

_HOST = "127.0.0.1:2456"
_ORIGIN = "http://127.0.0.1:2456"


def _admin_headers() -> dict[str, str]:
    return {
        "Host": _HOST,
        "Origin": _ORIGIN,
        "Authorization": f"Bearer {ADMIN_TOKEN}",
    }


def test_http_upstream_create_returns_exact_safe_disabled_row(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one unchanged synthetic upstream credential.
    credential = " opaque-http-é-key "

    # When: the administration route persists it.
    response = credential_http_slice.client.post(
        "/admin/api/v1/upstream-keys",
        headers=_admin_headers(),
        json={"key": credential},
    )

    # Then: status and DTO are exact while plaintext and vault fields stay absent.
    created = UpstreamKeyRead.model_validate_json(response.content)
    assert response.status_code == 201
    assert created.enabled is False
    assert created.fingerprint == f"sha256:{hashlib.sha256(credential.encode()).hexdigest()}"
    assert credential not in response.text
    assert "ciphertext" not in response.text
    assert "nonce" not in response.text


def test_http_upstream_list_is_complete_and_stably_ordered(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: two safe repository rows with the same creation timestamp.
    _ = anyio.run(
        credential_http_slice.repositories.upstream.create,
        UpstreamKeyCreateRequest(key="http-list-a"),
        "http-list-a",
    )
    _ = anyio.run(
        credential_http_slice.repositories.upstream.create,
        UpstreamKeyCreateRequest(key="http-list-b"),
        "http-list-b",
    )

    # When: the administration list route is called without query input.
    response = credential_http_slice.client.get(
        "/admin/api/v1/upstream-keys",
        headers=_admin_headers(),
    )

    # Then: every item is safe and ordered by creation time then UUID.
    listed = UpstreamKeyListResponse.model_validate_json(response.content)
    order = tuple((item.created_at, item.id.int) for item in listed.items)
    assert response.status_code == 200
    assert len(listed.items) == 2
    assert order == tuple(sorted(order))


def test_http_enabled_upstream_delete_is_a_safe_conflict(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one enabled key prepared through the product repository.
    created = anyio.run(
        credential_http_slice.repositories.upstream.create,
        UpstreamKeyCreateRequest(key="http-enabled-delete"),
        "http-enabled-create",
    )
    anyio.run(
        credential_http_slice.repositories.upstream.enable,
        created.id,
        "http-enabled-enable",
    )

    # When: the administration route tries to delete it.
    response = credential_http_slice.client.delete(
        f"/admin/api/v1/upstream-keys/{created.id}",
        headers=_admin_headers(),
    )

    # Then: the state conflict is stable and contains no internal state.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 409
    assert error.error.code == "resource_conflict"
    assert "enabled" not in response.text


def test_http_downstream_issue_is_one_time_and_list_is_tokenless(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one exact binary-unique label and reverse-order scope set.
    payload = {"label": "http-token", "scopes": ["chat:write", "models:read"]}

    # When: the administration route issues the bearer.
    response = credential_http_slice.client.post(
        "/admin/api/v1/downstream-tokens",
        headers=_admin_headers(),
        json=payload,
    )

    # Then: the one-time DTO is canonical and later product listing omits plaintext.
    issued = DownstreamTokenIssued.model_validate_json(response.content)
    assert response.status_code == 201
    assert issued.scopes == (DownstreamScope.MODELS_READ, DownstreamScope.CHAT_WRITE)
    listed = credential_http_slice.client.get(
        "/admin/api/v1/downstream-tokens",
        headers=_admin_headers(),
    )
    persisted = DownstreamTokenListResponse.model_validate_json(listed.content)
    assert issued.token not in listed.text
    assert "token" not in persisted.items[0].model_dump()


def test_http_repeated_downstream_revoke_is_not_found(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one token already revoked through the product repository.
    issued = anyio.run(
        credential_http_slice.repositories.downstream.issue,
        DownstreamTokenIssueRequest(label="http-revoked", scopes=(DownstreamScope.MODELS_READ,)),
        "http-revoked-issue",
    )
    anyio.run(
        credential_http_slice.repositories.downstream.revoke,
        issued.id,
        "http-revoked-first",
    )

    # When: the exact revoke route is repeated.
    response = credential_http_slice.client.delete(
        f"/admin/api/v1/downstream-tokens/{issued.id}",
        headers=_admin_headers(),
    )

    # Then: the response is the locked safe not-found outcome.
    error = ErrorEnvelope.model_validate_json(response.content)
    assert response.status_code == 404
    assert error.error.code == "resource_not_found"


def test_http_admin_validation_replaces_framework_detail(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: an invalid credential body containing a rejected marker.
    rejected = "rejected\ncredential"

    # When: it reaches the authenticated administration boundary.
    response = credential_http_slice.client.post(
        "/admin/api/v1/upstream-keys",
        headers=_admin_headers(),
        json={"key": rejected},
    )

    # Then: the sole fixed safe 422 replaces field paths, input, and context.
    error = AdminValidationErrorResponse.model_validate_json(response.content)
    assert response.status_code == 422
    assert error.error.code == "invalid_request"
    assert rejected not in response.text
    assert "detail" not in response.text
    assert "input" not in response.text


def test_http_admin_read_rejects_unsupported_query(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: an authenticated complete-list route with unsupported pagination.

    # When: a query parameter reaches the route boundary.
    response = credential_http_slice.client.get(
        "/admin/api/v1/upstream-keys?limit=1",
        headers=_admin_headers(),
    )

    # Then: it fails through the same fixed safe validation DTO.
    error = AdminValidationErrorResponse.model_validate_json(response.content)
    assert response.status_code == 422
    assert error.error.code == "invalid_request"


def test_http_overview_and_events_use_closed_product_projections(
    credential_http_slice: CredentialHttpSlice,
) -> None:
    # Given: one persisted downstream issuance event.
    _ = anyio.run(
        credential_http_slice.repositories.downstream.issue,
        DownstreamTokenIssueRequest(label="http-event", scopes=(DownstreamScope.MODELS_READ,)),
        "http-event-issue",
    )

    # When: overview and event read routes are requested.
    overview_response = credential_http_slice.client.get(
        "/admin/api/v1/overview",
        headers=_admin_headers(),
    )
    events_response = credential_http_slice.client.get(
        "/admin/api/v1/events",
        headers=_admin_headers(),
    )

    # Then: both bodies parse only through their exact safe DTOs.
    overview = AdminOverviewRead.model_validate_json(overview_response.content)
    events = AdminEventListResponse.model_validate_json(events_response.content)
    assert overview_response.status_code == 200
    assert events_response.status_code == 200
    assert overview.downstream_tokens.total == 1
    assert events.items[0].event_type.value == "downstream_token_issued"
