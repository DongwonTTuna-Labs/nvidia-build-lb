import json

import pytest
from pydantic import JsonValue, ValidationError

from nvidia_build_lb.admin.schemas import (
    DownstreamScope,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    DownstreamTokenRead,
)


def test_downstream_issue_request_preserves_label_and_canonicalizes_scopes() -> None:
    # Given: an exact label and reverse-order complete scope set.
    label = "hermes-cutover:00000000-0000-4000-8000-00000000abcd"
    raw = {"label": label, "scopes": ["chat:write", "models:read"]}

    # When: the issue request crosses the typed boundary.
    request = DownstreamTokenIssueRequest.model_validate(raw)

    # Then: the label is unchanged and scope output uses the fixed order.
    assert request.label == label
    assert request.scopes == (DownstreamScope.MODELS_READ, DownstreamScope.CHAT_WRITE)


@pytest.mark.parametrize(
    "label",
    ["", " leading", "trailing ", "line\nbreak", "control\x00", "\ud800", "x" * 129, 7],
)
def test_downstream_issue_request_rejects_invalid_labels(label: JsonValue) -> None:
    # Given: one value outside the label contract.
    raw = {"label": label, "scopes": ["models:read"]}

    # When: it crosses the issue boundary.
    with pytest.raises(ValidationError) as captured:
        _ = DownstreamTokenIssueRequest.model_validate_json(json.dumps(raw))

    # Then: validation fails without matching the rejected value.
    assert captured.value.error_count() == 1


@pytest.mark.parametrize(
    "scopes",
    [
        [],
        ["models:read", "models:read"],
        ["models:write"],
        ["models:read", "chat:write", "models:read"],
    ],
)
def test_downstream_issue_request_rejects_invalid_scope_sets(scopes: list[str]) -> None:
    # Given: an invalid scope collection.
    raw = {"label": "operator-token", "scopes": scopes}

    # When: it crosses the issue boundary.
    with pytest.raises(ValidationError) as captured:
        _ = DownstreamTokenIssueRequest.model_validate_json(json.dumps(raw))

    # Then: the collection is rejected.
    assert captured.value.error_count() >= 1


def test_downstream_issue_request_forbids_unknown_fields() -> None:
    # Given: an otherwise valid issue request with one extra field.
    raw = {"label": "operator-token", "scopes": ["models:read"], "token": "forbidden"}

    # When: it crosses the strict boundary.
    with pytest.raises(ValidationError) as captured:
        _ = DownstreamTokenIssueRequest.model_validate_json(json.dumps(raw))

    # Then: the extra field is rejected.
    assert captured.value.error_count() == 1


def test_downstream_issued_response_has_exact_one_time_shape() -> None:
    # Given: an exact one-time response from the issuance boundary.
    raw = {
        "id": "00000000-0000-4000-8000-00000000abcd",
        "label": "operator-token",
        "scopes": ["models:read", "chat:write"],
        "token": f"nblb_ds_{'a' * 64}",
        "revoked_at": None,
        "request_count": 0,
        "last_used_at": None,
        "created_at": "2026-07-12T01:02:03Z",
    }

    # When: it crosses the one-time response boundary.
    response = DownstreamTokenIssued.model_validate_json(json.dumps(raw))

    # Then: the approved response is preserved exactly.
    assert response.model_dump(mode="json") == raw


def test_downstream_read_and_list_never_accept_plaintext_token() -> None:
    # Given: the exact digest-free read shape and a plaintext-bearing variant.
    safe = {
        "id": "00000000-0000-4000-8000-00000000abcd",
        "label": "operator-token",
        "scopes": ["models:read"],
        "revoked_at": None,
        "request_count": 0,
        "last_used_at": None,
        "created_at": "2026-07-12T01:02:03Z",
    }
    unsafe = {**safe, "token": f"nblb_ds_{'a' * 64}"}

    # When: the safe item/list and unsafe item cross their boundaries.
    item = DownstreamTokenRead.model_validate_json(json.dumps(safe))
    collection = DownstreamTokenListResponse.model_validate_json(json.dumps({"items": [safe]}))
    with pytest.raises(ValidationError) as captured:
        _ = DownstreamTokenRead.model_validate_json(json.dumps(unsafe))

    # Then: reads preserve safe fields and reject plaintext-bearing extras.
    assert item.model_dump(mode="json") == safe
    assert collection.items == (item,)
    assert captured.value.error_count() == 1
