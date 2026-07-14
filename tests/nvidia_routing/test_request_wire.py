"""Immutable and allowlisted NVIDIA origin and poll request construction."""

import pytest
from pydantic import SecretStr

from nvidia_build_lb.request_wire import (
    NvidiaAuthority,
    NvidiaWireRequest,
    RequestWireError,
    build_nvidia_request,
)

pytestmark = pytest.mark.nvidia_routing

_BODY = b'{"model":"z-ai/glm-5.2","messages":[{"role":"user","content":"ping"}]}'


def test_origin_request_has_only_the_four_ordered_application_headers() -> None:
    request = build_nvidia_request(credential=SecretStr("synthetic-secret"), body=_BODY)

    assert request.method == "POST"
    assert request.authority is NvidiaAuthority.ORIGIN
    assert request.path == "/v1/chat/completions"
    assert request.body == _BODY
    assert request.application_header_names == (
        "Authorization",
        "Content-Type",
        "Accept",
        "Accept-Encoding",
    )
    assert request.application_headers()[1:] == (
        (b"Content-Type", b"application/json"),
        (b"Accept", b"application/json, text/event-stream"),
        (b"Accept-Encoding", b"identity"),
    )


def test_origin_authorization_uses_unchanged_utf8_credential_bytes() -> None:
    request = build_nvidia_request(credential=SecretStr("opaque-é"), body=_BODY)

    assert request.application_headers()[0] == (
        b"Authorization",
        b"Bearer opaque-\xc3\xa9",
    )


def test_poll_request_reuses_the_same_secret_without_body_or_encoder() -> None:
    credential = SecretStr("synthetic-secret")
    request = build_nvidia_request(
        credential=credential,
        body=None,
        origin_request_id="request_safe-1",
    )

    assert request.method == "GET"
    assert request.authority is NvidiaAuthority.POLL
    assert request.path == "/v2/nvcf/pexec/status/request_safe-1"
    assert request.body is None
    assert request.application_header_names == (
        "Authorization",
        "Accept",
        "Accept-Encoding",
    )
    assert request.credential is credential


def test_wire_request_repr_masks_secret_and_body() -> None:
    request = build_nvidia_request(credential=SecretStr("never-in-repr"), body=_BODY)

    rendered = repr(request)
    assert "never-in-repr" not in rendered
    assert "messages" not in rendered
    assert "**********" not in rendered


@pytest.mark.parametrize(
    "request_id",
    ["", "-bad", "bad/value", "bad,value", "a" * 129, "한글"],
)
def test_poll_request_id_uses_the_same_strict_202_grammar(request_id: str) -> None:
    with pytest.raises(RequestWireError):
        _ = build_nvidia_request(
            credential=SecretStr("synthetic-secret"),
            body=None,
            origin_request_id=request_id,
        )


def test_origin_requires_body_and_poll_forbids_body() -> None:
    with pytest.raises(RequestWireError):
        _ = build_nvidia_request(credential=SecretStr("synthetic-secret"), body=None)
    with pytest.raises(RequestWireError):
        _ = NvidiaWireRequest.poll(
            credential=SecretStr("synthetic-secret"),
            origin_request_id="request-1",
            body=b"forbidden",
        )


def test_invalid_unicode_scalar_error_retains_no_secret_exception_context() -> None:
    with pytest.raises(RequestWireError) as captured:
        _ = build_nvidia_request(credential=SecretStr("synthetic-\ud800"), body=_BODY)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
