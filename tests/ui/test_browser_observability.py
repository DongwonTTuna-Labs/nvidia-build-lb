from dataclasses import dataclass
from typing import Never, final

import pytest

from .browser_observability import (
    BrowserLogObservation,
    PageAudit,
    PhaseKind,
    RequestFailureObservation,
)
from .browser_observability_contract import (
    BrowserObservabilityError,
    authenticated_observability_receipt,
)
from .fake_admin_state import SafeNetworkObservation

pytestmark = pytest.mark.ui_fake

_CONSOLE_TEXT_ERROR = "console text must not be read"
_CONSOLE_ARGUMENTS_ERROR = "console arguments must not be read"
_REQUEST_URL_ERROR = "full request URL must not be read"
_RESPONSE_URL_ERROR = "full response URL must not be read"
_RESPONSE_HEADERS_ERROR = "response headers must not be read"
_RESPONSE_BODY_ERROR = "response body must not be read"


@final
class _ContentPoisonedConsoleMessage:
    type = "error"

    @property
    def text(self) -> Never:
        raise AssertionError(_CONSOLE_TEXT_ERROR)

    @property
    def args(self) -> Never:
        raise AssertionError(_CONSOLE_ARGUMENTS_ERROR)


@final
class _UrlPoisonedRequest:
    method = "GET"

    @property
    def url(self) -> Never:
        raise AssertionError(_REQUEST_URL_ERROR)


@dataclass(frozen=True, slots=True)
class _ContentPoisonedResponse:
    request: _UrlPoisonedRequest

    @property
    def url(self) -> Never:
        raise AssertionError(_RESPONSE_URL_ERROR)

    @property
    def headers(self) -> Never:
        raise AssertionError(_RESPONSE_HEADERS_ERROR)

    @property
    def body(self) -> Never:
        raise AssertionError(_RESPONSE_BODY_ERROR)


def _accepted_audit() -> tuple[PageAudit, tuple[SafeNetworkObservation, ...]]:
    audit = PageAudit()
    audit.page_console_calls.extend(
        (
            PhaseKind(phase="auth_wrong_token", kind="error"),
            PhaseKind(phase="auth_initial_503", kind="error"),
            PhaseKind(phase="offline_refresh", kind="error"),
        )
    )
    audit.browser_logs.extend(
        (
            BrowserLogObservation(
                phase="auth_wrong_token",
                source="network",
                level="error",
            ),
            BrowserLogObservation(
                phase="auth_initial_503",
                source="network",
                level="error",
            ),
            BrowserLogObservation(
                phase="offline_refresh",
                source="network",
                level="error",
            ),
        )
    )
    for _ in range(2):
        request = _UrlPoisonedRequest()
        audit.observe_request(request)
        audit.observe_response(_ContentPoisonedResponse(request=request))
    failed_request = _UrlPoisonedRequest()
    audit.set_phase("offline_refresh")
    audit.observe_request(failed_request)
    audit.observe_request_failed(failed_request)
    server_audit = (
        SafeNetworkObservation(
            method="GET",
            path="/admin/api/v1/overview",
            status=401,
            query_present=False,
        ),
        SafeNetworkObservation(
            method="GET",
            path="/admin/api/v1/overview",
            status=503,
            query_present=False,
        ),
    )
    return audit, server_audit


def test_observability_accepts_only_the_three_required_network_diagnostics() -> None:
    # Given: the required 401, initial-503, and offline phases with no application error.
    audit, server_audit = _accepted_audit()

    # When: the authenticated observability contract is evaluated.
    receipt = authenticated_observability_receipt(audit, server_audit)

    # Then: application errors stay zero and all three network diagnostics are explicit.
    assert receipt.application_console_errors == 0
    assert receipt.user_agent_network_errors == 3
    assert [item.phase for item in receipt.expected_browser_logs] == [
        "auth_wrong_token",
        "auth_initial_503",
        "offline_refresh",
    ]


def test_observability_rejects_an_unexpected_browser_network_log() -> None:
    # Given: an otherwise accepted audit plus a network error in another phase.
    audit, server_audit = _accepted_audit()
    audit.browser_logs.append(
        BrowserLogObservation(phase="cjk_xss_state", source="network", level="error")
    )

    # When/Then: the unexpected user-agent diagnostic blocks the receipt.
    with pytest.raises(BrowserObservabilityError, match="contract mismatch"):
        _ = authenticated_observability_receipt(audit, server_audit)


def test_observability_rejects_an_application_console_error() -> None:
    # Given: an otherwise accepted audit plus an application-authored error call.
    audit, server_audit = _accepted_audit()
    audit.runtime_console_calls.append(PhaseKind(phase="downstream_journey", kind="error"))

    # When/Then: the application console call blocks the receipt.
    with pytest.raises(BrowserObservabilityError, match="contract mismatch"):
        _ = authenticated_observability_receipt(audit, server_audit)


def test_observability_rejects_security_or_javascript_browser_errors() -> None:
    # Given: an otherwise accepted audit plus a security-source error.
    audit, server_audit = _accepted_audit()
    audit.browser_logs.append(
        BrowserLogObservation(phase="auth_shell", source="security", level="error")
    )

    # When/Then: CSP or JavaScript browser errors block the receipt.
    with pytest.raises(BrowserObservabilityError, match="contract mismatch"):
        _ = authenticated_observability_receipt(audit, server_audit)


def test_observability_rejects_a_request_not_accounted_for_by_the_fake_server() -> None:
    # Given: one browser request has neither a fake-server response nor an expected failure.
    audit, server_audit = _accepted_audit()
    audit.request_count += 1

    # When/Then: a potentially external request blocks the receipt.
    with pytest.raises(BrowserObservabilityError, match="contract mismatch"):
        _ = authenticated_observability_receipt(audit, server_audit)


def test_page_audit_never_reads_console_content_or_full_failed_request_url() -> None:
    # Given: event objects whose secret-bearing content and full URL fail on access.
    audit = PageAudit()
    audit.set_phase("offline_refresh")
    request = _UrlPoisonedRequest()

    # When: only the allowlisted diagnostic kind and failed-request method are projected.
    audit.observe_console(_ContentPoisonedConsoleMessage())
    audit.observe_request(request)
    audit.observe_response(_ContentPoisonedResponse(request=request))
    audit.observe_request_failed(request)

    # Then: no poison property was touched and only safe fields remain.
    assert audit.page_console_calls == [PhaseKind(phase="offline_refresh", kind="error")]
    assert audit.request_failures == [
        RequestFailureObservation(phase="offline_refresh", method="GET")
    ]
    assert audit.unanswered_request_failures() == ()
