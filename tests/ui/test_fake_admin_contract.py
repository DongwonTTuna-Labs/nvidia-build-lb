from uuid import UUID

import pytest

from nvidia_build_lb.admin.schemas import (
    DownstreamScope,
    DownstreamTokenIssueRequest,
    HealthState,
    ProbeStatus,
    UpstreamKeyCreateRequest,
)

from .fake_admin_state import FakeAdminError, FakeAdminState

pytestmark = pytest.mark.ui_fake


def test_fake_state_preserves_populated_contract_when_reset() -> None:
    # Given: the deterministic populated fixture.
    state = FakeAdminState()

    # When: safe projections are read.
    overview = state.overview()
    upstreams = state.upstreams().items
    tokens = state.tokens().items

    # Then: the exact state vocabulary and aggregates stay stable.
    assert overview.ready is False
    assert overview.upstream_keys.total == 2
    assert overview.upstream_keys.cooling == 1
    assert overview.downstream_tokens.revoked == 1
    assert upstreams[0].health_state is HealthState.UNKNOWN
    assert tokens[1].revoked_at is not None


def test_fake_upstream_lifecycle_keeps_plaintext_out_of_safe_projection() -> None:
    # Given: one synthetic credential that is not part of the seed state.
    state = FakeAdminState()
    request = UpstreamKeyCreateRequest(key="synthetic-fixture-key")

    # When: it is added, probed, enabled, disabled, and deleted.
    created = state.add_upstream(request)
    probe = state.change_upstream(created.id, "probe")
    _ = state.change_upstream(created.id, "enable")
    enabled = next(item for item in state.upstreams().items if item.id == created.id)
    _ = state.change_upstream(created.id, "disable")
    _ = state.change_upstream(created.id, "delete")

    # Then: transitions match the locked fake contract without retaining plaintext.
    assert probe is not None
    assert probe.probe_status is ProbeStatus.VALID
    assert enabled.enabled is True
    assert request.key not in state.upstreams().model_dump_json()
    assert all(item.id != created.id for item in state.upstreams().items)


def test_fake_downstream_issue_and_revoke_exposes_plaintext_once() -> None:
    # Given: a unique safe label and exact scopes.
    state = FakeAdminState()
    request = DownstreamTokenIssueRequest(
        label="Browser fixture",
        scopes=(DownstreamScope.MODELS_READ, DownstreamScope.CHAT_WRITE),
    )

    # When: a token is issued and then revoked.
    issued = state.issue_token(request)
    before_revoke = state.tokens().model_dump_json()
    state.revoke_token(issued.id)
    after_revoke = next(item for item in state.tokens().items if item.id == issued.id)

    # Then: list state is token-free and repeated revocation is a safe 404.
    assert issued.token not in before_revoke
    assert after_revoke.revoked_at is not None
    with pytest.raises(FakeAdminError) as caught:
        state.revoke_token(issued.id)
    assert caught.value.status_code == 404


def test_fake_state_consumes_one_exact_unavailable_failure() -> None:
    # Given: one exact route is configured to fail once.
    state = FakeAdminState()
    path = "/admin/api/v1/overview"
    state.fail_next(path)

    # When: availability is checked twice.
    with pytest.raises(FakeAdminError) as caught:
        state.check_available(path)
    state.check_available(path)

    # Then: only the first check fails with the safe stable class.
    assert caught.value.status_code == 503
    assert caught.value.code == "database_unavailable"


def test_fake_state_rejects_unknown_resource_without_mutation() -> None:
    # Given: an ID outside the deterministic state.
    state = FakeAdminState()
    unknown = UUID("00000000-0000-4000-8000-ffffffffffff")
    before = state.upstreams()

    # When: a row action targets that ID.
    with pytest.raises(FakeAdminError) as caught:
        _ = state.change_upstream(unknown, "probe")

    # Then: the safe 404 is exact and state is unchanged.
    assert caught.value.status_code == 404
    assert state.upstreams() == before
