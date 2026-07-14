from collections import Counter
from re import fullmatch
from typing import Protocol, final, override

from .browser_evidence import BrowserLogCount, BrowserObservabilityReceipt
from .browser_observability import (
    BrowserLogObservation,
    PhaseKind,
    RequestFailureObservation,
)
from .fake_admin_state import SafeNetworkObservation


class _AuditLike(Protocol):
    browser_logs: list[BrowserLogObservation]
    page_console_calls: list[PhaseKind]
    page_errors: list[str]
    request_count: int
    request_failures: list[RequestFailureObservation]
    response_count: int
    runtime_console_calls: list[PhaseKind]
    runtime_exceptions: list[str]

    def unanswered_request_failures(self) -> tuple[RequestFailureObservation, ...]: ...


@final
class BrowserObservabilityError(Exception):
    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason


_EXPECTED_NETWORK_LOGS = Counter(
    {
        ("auth_wrong_token", "network", "error"): 1,
        ("auth_initial_503", "network", "error"): 1,
        ("offline_refresh", "network", "error"): 1,
    }
)
_PROBLEM_LEVELS = frozenset({"error", "warning"})
_PROBLEM_CONSOLE_TYPES = frozenset({"assert", "error", "warning"})


def network_observation_is_allowed(item: SafeNetworkObservation) -> bool:
    exact = {
        ("GET", "/admin", 200),
        ("GET", "/showcase", 200),
        ("GET", "/assets/admin.css", 200),
        ("GET", "/assets/admin.js", 200),
        ("GET", "/assets/showcase.css", 200),
        ("GET", "/admin/api/v1/overview", 200),
        ("GET", "/admin/api/v1/overview", 401),
        ("GET", "/admin/api/v1/overview", 503),
        ("GET", "/admin/api/v1/upstream-keys", 200),
        ("POST", "/admin/api/v1/upstream-keys", 201),
        ("GET", "/admin/api/v1/downstream-tokens", 200),
        ("POST", "/admin/api/v1/downstream-tokens", 201),
        ("GET", "/admin/api/v1/events", 200),
    }
    if (item.method, item.path, item.status) in exact:
        return True
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    upstream_pattern = rf"/admin/api/v1/upstream-keys/({uuid})(?:/(enable|disable|probe))?"
    upstream = fullmatch(upstream_pattern, item.path)
    if upstream is not None:
        action = upstream.group(2)
        return (item.method, action, item.status) in {
            ("DELETE", None, 204),
            ("POST", "enable", 204),
            ("POST", "disable", 204),
            ("POST", "probe", 200),
        }
    downstream = fullmatch(rf"/admin/api/v1/downstream-tokens/{uuid}", item.path)
    return downstream is not None and (item.method, item.status) == ("DELETE", 204)


def assert_public_observability_clean(audit: _AuditLike) -> None:
    problem_logs = [item for item in audit.browser_logs if item.level in _PROBLEM_LEVELS]
    problem_calls = [
        item for item in audit.runtime_console_calls if item.kind in _PROBLEM_CONSOLE_TYPES
    ]
    if problem_logs or problem_calls or audit.runtime_exceptions or audit.page_errors:
        reason = "public browser observability contained an error or warning"
        raise BrowserObservabilityError(reason)


def authenticated_observability_receipt(
    audit: _AuditLike,
    server_audit: tuple[SafeNetworkObservation, ...],
) -> BrowserObservabilityReceipt:
    application_errors = sum(
        item.kind in _PROBLEM_CONSOLE_TYPES for item in audit.runtime_console_calls
    )
    browser_problem_logs = Counter(
        (item.phase, item.source, item.level)
        for item in audit.browser_logs
        if item.level in _PROBLEM_LEVELS
    )
    page_problem_calls = Counter(
        (item.phase, item.kind)
        for item in audit.page_console_calls
        if item.kind in _PROBLEM_CONSOLE_TYPES
    )
    expected_page_calls = Counter(
        {
            ("auth_wrong_token", "error"): 1,
            ("auth_initial_503", "error"): 1,
            ("offline_refresh", "error"): 1,
        }
    )
    csp_or_javascript = sum(
        item.source in {"javascript", "security"} and item.level in _PROBLEM_LEVELS
        for item in audit.browser_logs
    )
    wrong_auth = sum(
        item.method == "GET" and item.path == "/admin/api/v1/overview" and item.status == 401
        for item in server_audit
    )
    initial_503 = sum(
        item.method == "GET" and item.path == "/admin/api/v1/overview" and item.status == 503
        for item in server_audit
    )
    unanswered_failures = audit.unanswered_request_failures()
    offline_failures = sum(
        item.phase == "offline_refresh" and item.method == "GET" for item in unanswered_failures
    )
    failed_request_kinds = Counter((item.phase, item.method) for item in unanswered_failures)
    network_accounting_matches = audit.response_count == len(
        server_audit
    ) and audit.request_count == audit.response_count + len(unanswered_failures)
    query_entries = sum(item.query_present for item in server_audit)
    disallowed_paths = sum(not network_observation_is_allowed(item) for item in server_audit)
    server_projection_is_safe = query_entries == 0 and disallowed_paths == 0
    contract_matches = (
        application_errors == 0
        and not audit.runtime_exceptions
        and not audit.page_errors
        and csp_or_javascript == 0
        and browser_problem_logs == _EXPECTED_NETWORK_LOGS
        and page_problem_calls == expected_page_calls
        and wrong_auth == 1
        and initial_503 == 1
        and offline_failures == 1
        and network_accounting_matches
        and server_projection_is_safe
    )
    if not contract_matches:
        reason = (
            "browser observability contract mismatch: "
            f"application={application_errors}, runtime={len(audit.runtime_exceptions)}, "
            f"page={len(audit.page_errors)}, browser={sum(browser_problem_logs.values())}, "
            f"user_agent={sum(page_problem_calls.values())}, auth401={wrong_auth}, "
            f"initial503={initial_503}, offline={offline_failures}, "
            f"requests={audit.request_count}, responses={audit.response_count}, "
            f"server={len(server_audit)}, queries={query_entries}, "
            f"disallowed={disallowed_paths}, csp_js={csp_or_javascript}, "
            f"browser_exact={browser_problem_logs == _EXPECTED_NETWORK_LOGS}, "
            f"user_agent_exact={page_problem_calls == expected_page_calls}, "
            f"accounted={network_accounting_matches}, failures={failed_request_kinds}"
        )
        raise BrowserObservabilityError(reason)
    expected_logs = (
        BrowserLogCount(
            phase="auth_wrong_token",
            source="network",
            level="error",
            count=1,
        ),
        BrowserLogCount(
            phase="auth_initial_503",
            source="network",
            level="error",
            count=1,
        ),
        BrowserLogCount(
            phase="offline_refresh",
            source="network",
            level="error",
            count=1,
        ),
    )
    return BrowserObservabilityReceipt(
        application_console_errors=0,
        runtime_exceptions=0,
        page_errors=0,
        csp_or_javascript_errors=0,
        unexpected_browser_logs=0,
        user_agent_network_errors=3,
        wrong_auth_401_responses=1,
        initial_503_responses=1,
        offline_get_failed_requests=1,
        browser_request_events=audit.request_count,
        browser_response_events=audit.response_count,
        fake_server_response_events=len(server_audit),
        network_accounting_matches=True,
        expected_browser_logs=expected_logs,
    )
