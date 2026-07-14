"""Exact successful administration API response contracts."""

import hashlib

from nvidia_build_lb.admin.schemas import (
    AdminEventListResponse,
    AdminOverviewRead,
    DownstreamScope,
    DownstreamTokenIssued,
    DownstreamTokenListResponse,
    EventOutcome,
    EventType,
    HealthState,
    ProbeStatus,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)

from ._support import (
    ADMIN_TOKEN,
    DISABLED_KEY_ID,
    DOWNSTREAM_ID,
    UPSTREAM_KEY,
    ContractClient,
    bearer,
)


def test_upstream_create_returns_the_exact_disabled_initial_state(
    contract_client: ContractClient,
) -> None:
    # Given: an opaque synthetic upstream credential.
    expected_fingerprint = f"sha256:{hashlib.sha256(UPSTREAM_KEY.encode()).hexdigest()}"

    # When: the admin creates its encrypted key row.
    response = contract_client.request(
        "POST",
        "/admin/api/v1/upstream-keys",
        headers=bearer(ADMIN_TOKEN),
        json_body={"key": UPSTREAM_KEY},
    )

    # Then: the response is the exact secret-free initial DTO.
    assert response.status_code == 201
    payload = UpstreamKeyRead.model_validate_json(response.content)
    assert payload.fingerprint == expected_fingerprint
    assert payload.enabled is False
    assert payload.health_state is HealthState.UNKNOWN
    assert payload.cooldown_until is None
    assert payload.request_count == 0
    assert payload.success_count == 0
    assert payload.failure_count == 0
    assert payload.last_status_class is None
    assert payload.last_used_at is None
    assert payload.created_at == payload.updated_at


def test_upstream_list_returns_all_rows_in_exact_stable_order(
    contract_client: ContractClient,
) -> None:
    # Given: the fixture's two safe upstream rows.
    expected_ids = {
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    }

    # When: the complete nonpaginated list is requested.
    response = contract_client.request(
        "GET",
        "/admin/api/v1/upstream-keys",
        headers=bearer(ADMIN_TOKEN),
    )

    # Then: exact safe items are ordered by creation time and ID.
    assert response.status_code == 200
    payload = UpstreamKeyListResponse.model_validate_json(response.content)
    assert {str(item.id) for item in payload.items} == expected_ids
    order = tuple((item.created_at, item.id.int) for item in payload.items)
    assert order == tuple(sorted(order))


def test_upstream_probe_returns_only_the_four_value_probe_contract(
    contract_client: ContractClient,
) -> None:
    # Given: a disabled key that must remain disabled.
    path = f"/admin/api/v1/upstream-keys/{DISABLED_KEY_ID}/probe"

    # When: that exact key alone is probed.
    response = contract_client.request("POST", path, headers=bearer(ADMIN_TOKEN))

    # Then: the exact result reports validity without enabling it.
    assert response.status_code == 200
    payload = UpstreamProbeResponse.model_validate_json(response.content)
    assert str(payload.id) == DISABLED_KEY_ID
    assert payload.enabled is False
    assert payload.probe_status is ProbeStatus.VALID


def test_downstream_issue_returns_plaintext_once_with_canonical_scopes(
    contract_client: ContractClient,
) -> None:
    # Given: a durable Hermes attempt label and reverse-order exact scopes.
    label = "hermes-cutover:00000000-0000-4000-8000-00000000abcd"

    # When: the admin issues one new downstream token.
    response = contract_client.request(
        "POST",
        "/admin/api/v1/downstream-tokens",
        headers=bearer(ADMIN_TOKEN),
        json_body={"label": label, "scopes": ["chat:write", "models:read"]},
    )

    # Then: plaintext appears in the exact one-time DTO and counters start at zero.
    assert response.status_code == 201
    payload = DownstreamTokenIssued.model_validate_json(response.content)
    assert payload.label == label
    assert payload.scopes == (DownstreamScope.MODELS_READ, DownstreamScope.CHAT_WRITE)
    assert payload.revoked_at is None
    assert payload.request_count == 0
    assert payload.last_used_at is None


def test_downstream_list_returns_tokenless_rows_in_exact_stable_order(
    contract_client: ContractClient,
) -> None:
    # Given: the fixture's active and revoked persisted token rows.

    # When: the complete nonpaginated token list is requested.
    response = contract_client.request(
        "GET",
        "/admin/api/v1/downstream-tokens",
        headers=bearer(ADMIN_TOKEN),
    )

    # Then: the one-time token field is impossible and ordering is stable.
    assert response.status_code == 200
    payload = DownstreamTokenListResponse.model_validate_json(response.content)
    assert DOWNSTREAM_ID in {str(item.id) for item in payload.items}
    order = tuple((item.created_at, item.id.int) for item in payload.items)
    assert order == tuple(sorted(order))


def test_overview_returns_the_exact_seeded_aggregate_contract(
    contract_client: ContractClient,
) -> None:
    # Given: deterministic seeded key, token, request, and event state.
    expected_upstream = {"total": 2, "enabled": 2, "eligible": 1, "cooling": 1, "degraded": 1}
    expected_downstream = {"total": 3, "active": 2, "revoked": 1}

    # When: the operational overview is requested.
    response = contract_client.request(
        "GET",
        "/admin/api/v1/overview",
        headers=bearer(ADMIN_TOKEN),
    )

    # Then: exact readiness and aggregate fields match the seeded state.
    assert response.status_code == 200
    payload = AdminOverviewRead.model_validate_json(response.content)
    assert payload.status.value == "ok"
    assert payload.ready is True
    assert payload.upstream_keys.model_dump(mode="json") == expected_upstream
    assert payload.downstream_tokens.model_dump(mode="json") == expected_downstream
    assert payload.request_count == 42
    assert payload.last_event_at is not None


def test_events_returns_only_the_latest_one_hundred_in_descending_order(
    contract_client: ContractClient,
) -> None:
    # Given: a deterministic event history longer than the response bound.

    # When: the nonpaginated recent-event route is requested.
    response = contract_client.request(
        "GET",
        "/admin/api/v1/events",
        headers=bearer(ADMIN_TOKEN),
    )

    # Then: exact safe event DTOs are newest-first and capped at one hundred.
    assert response.status_code == 200
    payload = AdminEventListResponse.model_validate_json(response.content)
    assert len(payload.items) == 100
    order = tuple((item.occurred_at, item.id.int) for item in payload.items)
    assert order == tuple(sorted(order, reverse=True))
    assert all(item.event_type in EventType for item in payload.items)
    assert all(item.outcome_class in EventOutcome for item in payload.items)
