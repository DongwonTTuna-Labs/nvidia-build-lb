"""Exact browser sensors for decision-state regressions found during review."""

from collections.abc import Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest
from playwright.sync_api import APIResponse, BrowserContext, Page, Route, expect

from nvidia_build_lb.admin.schemas import (
    AdminDashboardEventListResponse,
    AdminDashboardEventRead,
    AdminDashboardRead,
    CapacityBlocker,
    DownstreamScope,
    DownstreamTokenIssueRequest,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    LedgerStatus,
    OverviewStatus,
    ReadinessCause,
    RuntimeState,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
)

from .browser_checks import axe_counts, evaluate_string, execute_script
from .browser_runtime import (
    UI_ORIGIN,
    RunningFakeServer,
    start_fake_server,
    start_managed_browser,
    stop_fake_server,
    stop_managed_browser,
)
from .test_decision_journey import (
    FIRST_KEY_ID,
    enable_first_key,
    install_decision_scenario,
    live_changes,
    observe_live_nodes,
    project_upstreams_for_scenario,
)

pytestmark = pytest.mark.ui_fake
_AXE_ASSET = Path(__file__).parent / "vendor" / "axe-core-4.12.1" / "axe.min.js"
_UNAUTHORIZED_BODY = (
    '{"error":{"code":"admin_unauthorized","message":"admin authentication required",'
    '"request_id":"synthetic-unauthorized"}}'
)


def _replace_dashboard_upstreams(
    payload: AdminDashboardRead,
    upstreams: UpstreamKeyListResponse,
) -> AdminDashboardRead:
    items = upstreams.items
    eligible = sum(item.routing_state.value == "eligible" for item in items)
    cooling = sum(item.routing_state.value == "cooldown" for item in items)
    degraded = sum(item.health_state is HealthState.DEGRADED for item in items)
    runtime_unavailable = payload.runtime_state.value == "unavailable"
    capacity_blocked = payload.ledger.status.value in {
        "capacity_exhausted_recovering",
        "capacity_blocked",
    }
    readiness = (
        ReadinessCause.RUNTIME_UNAVAILABLE
        if runtime_unavailable
        else ReadinessCause.LEDGER_CAPACITY_EXHAUSTED
        if capacity_blocked
        else ReadinessCause.NO_ELIGIBLE_UPSTREAM
        if eligible == 0
        else ReadinessCause.READY
    )
    overview = payload.overview.model_copy(
        update={
            "status": (
                OverviewStatus.OK if readiness is ReadinessCause.READY else OverviewStatus.DEGRADED
            ),
            "ready": readiness is ReadinessCause.READY,
            "upstream_keys": payload.overview.upstream_keys.model_copy(
                update={
                    "total": len(items),
                    "enabled": sum(item.enabled for item in items),
                    "eligible": eligible,
                    "cooling": cooling,
                    "degraded": degraded,
                }
            ),
        }
    )
    return payload.model_copy(
        update={
            "readiness_cause": readiness,
            "overview": overview,
            "upstream_keys": upstreams,
        }
    )


@pytest.fixture
def decision_browser() -> Iterator[tuple[RunningFakeServer, BrowserContext, Page]]:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        yield server, context, page
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def _login(page: Page, server: RunningFakeServer) -> None:
    _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")


def test_first_capacity_assessment_has_an_operational_reason_and_one_refresh(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)

    def awaiting_assessment(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        ledger = payload.ledger.model_copy(
            update={
                "status": LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING,
                "capacity_blocker": CapacityBlocker.NONE,
                "event_rows": payload.ledger.event_capacity,
                "reserved_terminal_slots": 0,
            }
        )
        overview = payload.overview.model_copy(
            update={"status": OverviewStatus.DEGRADED, "ready": False}
        )
        route.fulfill(
            response=response,
            body=payload.model_copy(
                update={
                    "readiness_cause": ReadinessCause.LEDGER_CAPACITY_EXHAUSTED,
                    "ledger": ledger,
                    "overview": overview,
                }
            ).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", awaiting_assessment, times=1)
    page.locator("#refresh-dashboard").click()

    expect(page.locator("#decision-explanation")).to_contain_text(
        "first automatic maintenance assessment"
    )
    expect(page.locator("#decision-explanation")).not_to_contain_text("at none")
    expect(page.locator("#recommended-action")).to_have_text("Refresh settlement evidence")
    expect(page.locator("#recommended-action")).to_be_visible()


@pytest.mark.parametrize(
    ("blocker", "cause"),
    [
        (CapacityBlocker.ORPHANED_PENDING, "unfinished attempt has no live owner evidence"),
        (CapacityBlocker.LEGACY_UNLINKED, "Legacy attempt evidence cannot be linked safely"),
    ],
)
def test_permanent_ledger_blocker_keeps_intake_withdrawn_without_cap_advice(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
    blocker: CapacityBlocker,
    cause: str,
) -> None:
    server, _, page = decision_browser
    _login(page, server)

    def blocked(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        ledger = payload.ledger.model_copy(
            update={
                "status": LedgerStatus.CAPACITY_BLOCKED,
                "capacity_blocker": blocker,
                "event_rows": 0,
                "attempt_rows": 0,
                "reserved_terminal_slots": 0,
            }
        )
        overview = payload.overview.model_copy(
            update={"status": OverviewStatus.DEGRADED, "ready": False}
        )
        route.fulfill(
            response=response,
            body=payload.model_copy(
                update={
                    "readiness_cause": ReadinessCause.LEDGER_CAPACITY_EXHAUSTED,
                    "ledger": ledger,
                    "overview": overview,
                }
            ).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", blocked, times=1)
    page.locator("#refresh-dashboard").click()

    expect(page.locator("#decision-explanation")).to_contain_text(cause)
    page.locator("#recommended-action").click()
    recovery = page.locator("#ledger-recovery")
    expect(recovery).to_contain_text("Keep the gateway withdrawn")
    expect(recovery).to_contain_text("reviewed forward repair")
    expect(recovery).to_contain_text("cap increases and restarts do not repair")
    expect(recovery).not_to_contain_text("Increase the configured")


def test_unconfirmed_operator_probe_locks_target_and_recommends_only_refresh(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)

    def unconfirmed_probe(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        item = payload.upstream_keys.items[0]
        event_time = datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)
        events = AdminDashboardEventListResponse(
            items=(
                AdminDashboardEventRead(
                    id=UUID("00000000-0000-4000-8000-000000000061"),
                    request_id="operator-probe-unconfirmed",
                    event_type=EventType.UPSTREAM_PROBE,
                    upstream_key_id=item.id,
                    downstream_token_id=None,
                    outcome_class=EventOutcome.STARTED,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=event_time,
                    upstream_key_fingerprint=item.fingerprint,
                    attempt_started_event_id=None,
                ),
            )
        )
        overview = payload.overview.model_copy(
            update={"last_event_at": event_time, "generated_at": event_time}
        )
        ledger = payload.ledger.model_copy(update={"event_rows": 1})
        route.fulfill(
            response=response,
            body=payload.model_copy(
                update={"events": events, "overview": overview, "ledger": ledger}
            ).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", unconfirmed_probe, times=1)
    page.locator("#refresh-dashboard").click()

    expect(page.locator("#activity-summary")).to_contain_text(
        "operator probe completion unconfirmed · Do not retry from this view"
    )
    expect(page.locator("#decision-title")).to_have_text("Wait for probe completion")
    recommendation = page.locator("#recommended-action")
    expect(recommendation).to_have_text("Refresh probe status")
    expect(recommendation).not_to_have_attribute("data-mutation", "true")
    reason_id = f"key-{FIRST_KEY_ID}-unconfirmed-probe-reason"
    expect(page.locator(f"#{reason_id}")).to_contain_text(
        "previous operator probe has no confirmed completion"
    )
    for suffix in ("probe", "toggle", "delete"):
        control = page.locator(f"#key-{FIRST_KEY_ID}-{suffix}")
        expect(control).to_be_disabled()
        expect(control).to_have_attribute("aria-describedby", reason_id)


def test_stale_maintenance_evidence_stops_claiming_current_capacity(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)

    def overdue(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        ledger = payload.ledger.model_copy(update={"status": LedgerStatus.MAINTENANCE_OVERDUE})
        route.fulfill(
            response=response,
            body=payload.model_copy(update={"ledger": ledger}).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", overdue, times=1)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#activity-summary")).to_contain_text("Capacity remains available")

    server.state.fail_next("/admin/api/v1/dashboard")
    page.locator("#refresh-dashboard").click()

    expect(page.locator("#activity-summary")).to_contain_text(
        "Current capacity and the next cleanup retry are unconfirmed"
    )
    expect(page.locator("#activity-summary")).not_to_contain_text("Capacity remains available")


@pytest.mark.parametrize("prior_cause", ["runtime", "capacity", "eligibility"])
def test_stale_issue_prerequisite_overrides_every_previous_current_cause(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
    prior_cause: str,
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    prior_state_generated_at = datetime(2031, 2, 3, 4, 5, 6, tzinfo=UTC)

    def prior_state(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        if prior_cause == "runtime":
            payload = payload.model_copy(
                update={
                    "runtime_state": RuntimeState.UNAVAILABLE,
                    "readiness_cause": ReadinessCause.RUNTIME_UNAVAILABLE,
                    "overview": payload.overview.model_copy(
                        update={"status": OverviewStatus.DEGRADED, "ready": False}
                    ),
                }
            )
        elif prior_cause == "capacity":
            payload = payload.model_copy(
                update={
                    "readiness_cause": ReadinessCause.LEDGER_CAPACITY_EXHAUSTED,
                    "ledger": payload.ledger.model_copy(
                        update={
                            "status": LedgerStatus.CAPACITY_BLOCKED,
                            "capacity_blocker": CapacityBlocker.ORPHANED_PENDING,
                            "event_rows": payload.ledger.event_capacity,
                            "reserved_terminal_slots": 0,
                        }
                    ),
                    "overview": payload.overview.model_copy(
                        update={"status": OverviewStatus.DEGRADED, "ready": False}
                    ),
                }
            )
        payload = payload.model_copy(
            update={
                "overview": payload.overview.model_copy(
                    update={"generated_at": prior_state_generated_at}
                )
            }
        )
        route.fulfill(response=response, body=payload.model_dump_json())

    _ = page.route("**/admin/api/v1/dashboard", prior_state, times=1)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#snapshot-evidence")).to_contain_text("2031-02-03T04:05:06")
    expect(page.locator("#issue-downstream-reason")).not_to_contain_text("Current state is unknown")
    page.unroute("**/admin/api/v1/dashboard", prior_state)

    server.state.fail_next("/admin/api/v1/dashboard")
    if prior_cause == "runtime":
        page.get_by_role("button", name="Refresh after service recovery", exact=True).click()
    else:
        page.locator("#refresh-dashboard").click()

    expect(page.locator("#issue-downstream-reason")).to_have_text(
        "Current state is unknown. Refresh before issuing a downstream token."
    )


def _issue_token(page: Page, label: str) -> None:
    page.locator("#issue-downstream").click()
    page.locator("#downstream-label").fill(label)
    page.locator("input[name='scope']").first.check()
    page.locator("#submit-downstream").click()
    expect(page.locator("#credential-dialog")).to_be_visible()


def _observe_busy_order(page: Page, control_id: str, busy_id: str) -> None:
    execute_script(
        page,
        f"""() => {{
globalThis.__busyOrder = [];
const control = document.getElementById('{control_id}');
document.addEventListener('focusin', (event) => {{
  if (event.target.id === '{busy_id}') globalThis.__busyOrder.push('focus');
}}, {{capture: true}});
new MutationObserver((records) => {{
  if (records.some((record) => record.attributeName === 'disabled' && record.oldValue === null)) {{
    globalThis.__busyOrder.push('disabled');
  }}
}}).observe(control, {{attributes: true, attributeOldValue: true}});
}}""",
    )


def _hold_committed_response(
    held: list[tuple[Route, APIResponse]],
    route: Route,
) -> None:
    held.append((route, route.fetch()))


def _hold_scenario_dashboard_response(
    held: list[tuple[Route, APIResponse, str]],
    scenario: str,
    route: Route,
) -> None:
    response = route.fetch()
    dashboard = AdminDashboardRead.model_validate_json(response.text())
    upstreams = project_upstreams_for_scenario(dashboard.upstream_keys, scenario)
    projected = _replace_dashboard_upstreams(dashboard, upstreams)
    held.append((route, response, projected.model_dump_json()))


def _pop_held_response(
    page: Page,
    held: list[tuple[Route, APIResponse]],
) -> tuple[Route, APIResponse]:
    for _ in range(100):
        if held:
            return held.pop()
        page.wait_for_timeout(10)
    detail = "intercepted response did not become available"
    raise AssertionError(detail)


def _pop_held_dashboard_response(
    page: Page,
    held: list[tuple[Route, APIResponse, str]],
) -> tuple[Route, APIResponse, str]:
    for _ in range(100):
        if held:
            return held.pop()
        page.wait_for_timeout(10)
    detail = "intercepted dashboard response did not become available"
    raise AssertionError(detail)


def test_unconfirmed_outcome_preserves_the_previous_confirmed_result(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    enable_first_key(page)

    def abort_add(route: Route) -> None:
        if route.request.method == "POST":
            route.abort()
        else:
            route.continue_()

    _ = page.route("**/admin/api/v1/upstream-keys", abort_add)
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill("synthetic-unconfirmed-separate-slot")
    page.locator("#submit-upstream").click()
    expect(page.locator("#upstream-error")).to_contain_text("was lost")
    expect(page.locator("#last-result")).to_contain_text("was enabled")
    expect(page.locator("#last-result")).not_to_contain_text("unknown")
    expect(page.locator("#operation-status-line")).to_be_visible()
    expect(page.locator("#operation-status")).to_contain_text("result is unknown")

    page.locator("[data-close='upstream-dialog']").click()
    page.unroute("**/admin/api/v1/upstream-keys", abort_add)
    page.locator("#recommended-action").click()
    expect(page.locator("#operation-status-line")).to_be_hidden()
    expect(page.locator("#last-result")).to_contain_text("does not contain the submitted key")
    expect(page.locator("#last-result")).not_to_contain_text("in progress")


def test_probe_401_is_known_no_success_after_reauthentication(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    probe_path = f"**/admin/api/v1/upstream-keys/{FIRST_KEY_ID}/probe"

    def unauthorized_probe(route: Route) -> None:
        route.fulfill(status=401, content_type="application/json", body=_UNAUTHORIZED_BODY)

    _ = page.route(probe_path, unauthorized_probe)
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#login-error")).to_be_visible()
    expect(page.locator("#login-interrupted")).to_contain_text("Probing Key aaaaaaaa")
    page.unroute(probe_path, unauthorized_probe)
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#last-result")).to_have_text("No action has completed in this tab.")
    expect(page.locator("#operation-status-line")).to_be_hidden()
    expect(page.locator("#decision-title")).to_have_text("Probe Key aaaaaaaa")


def test_confirmed_probe_survives_followup_refresh_401(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    gate = {"armed": True, "consumed": False}

    def one_unauthorized_overview(route: Route) -> None:
        if gate["armed"] and not gate["consumed"]:
            gate["consumed"] = True
            route.fulfill(status=401, content_type="application/json", body=_UNAUTHORIZED_BODY)
        else:
            route.continue_()

    _ = page.route("**/admin/api/v1/dashboard", one_unauthorized_overview)
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#login-error")).to_be_visible()
    expect(page.locator("#login-interrupted")).to_contain_text("last confirmed action")
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#last-result")).to_contain_text("Probe confirmed Key aaaaaaaa is valid")
    expect(page.locator("#operation-status-line")).to_be_hidden()
    expect(page.locator("#decision-title")).to_have_text("Enable Key aaaaaaaa")


def test_digest_failure_restores_field_focus_and_hides_busy_status(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    execute_script(
        page,
        """() => Object.defineProperty(globalThis.crypto.subtle, 'digest', {
  configurable: true,
  value: async () => { throw new DOMException('synthetic digest failure'); },
})""",
    )
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill("synthetic-digest-failure")
    page.locator("#submit-upstream").click()
    expect(page.locator("#upstream-key")).to_be_enabled()
    expect(page.locator("#upstream-key")).to_be_focused()
    expect(page.locator("#upstream-dialog-busy")).to_be_hidden()
    expect(page.locator("#upstream-error")).to_contain_text("could not create")


def test_dialog_busy_focus_precedes_disable_without_global_duplicate(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    server.state.fail_next("/admin/api/v1/upstream-keys")
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill("synthetic-busy-order")
    observe_live_nodes(page, ("upstream-dialog-busy", "live-region"))
    _observe_busy_order(page, "submit-upstream", "upstream-dialog-busy")
    page.locator("#submit-upstream").click()
    expect(page.locator("#upstream-error")).to_be_visible()
    busy_changes = live_changes(page)
    assert "Adding an upstream key" in busy_changes
    assert "Waiting for server confirmation" not in busy_changes
    assert (
        evaluate_string(page, "() => JSON.stringify(globalThis.__busyOrder.slice(0, 2))")
        == '["focus","disabled"]'
    )
    assert '"id":"live-region"' not in live_changes(page)
    page.locator("[data-close='upstream-dialog']").click()
    page.locator("#recommended-action").click()

    enable_first_key(page)
    page.locator(f"#key-{FIRST_KEY_ID}-toggle").click()
    observe_live_nodes(page, ("confirm-dialog-busy",))
    _observe_busy_order(page, "confirm-action", "confirm-dialog-busy")
    page.locator("#confirm-action").click()
    expect(page.locator("#confirm-dialog")).to_be_hidden()
    assert "Disabling Key aaaaaaaa" in live_changes(page)
    assert (
        evaluate_string(page, "() => JSON.stringify(globalThis.__busyOrder.slice(0, 2))")
        == '["focus","disabled"]'
    )


def test_dismissal_blocks_copy_and_401_reset_unlocks_the_next_credential(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    enable_first_key(page)
    _issue_token(page, "Clipboard race client")
    execute_script(
        page,
        """() => {
globalThis.__clipboardValue = 'synthetic-previous-value';
globalThis.__resolveClipboardRead = null;
Object.defineProperty(navigator.clipboard, 'writeText', {
  configurable: true,
  value: async (value) => { globalThis.__clipboardValue = value; },
});
Object.defineProperty(navigator.clipboard, 'readText', {
  configurable: true,
  value: () => new Promise((resolve) => {
    globalThis.__resolveClipboardRead = () => resolve(globalThis.__clipboardValue);
  }),
});
}""",
    )
    consumed = {"value": False}

    def unauthorized_refresh(route: Route) -> None:
        if not consumed["value"]:
            consumed["value"] = True
            route.fulfill(status=401, content_type="application/json", body=_UNAUTHORIZED_BODY)
        else:
            route.continue_()

    _ = page.route("**/admin/api/v1/dashboard", unauthorized_refresh)
    page.locator("#dismiss-token").click()
    expect(page.locator("#credential-busy")).to_be_focused()
    _ = page.wait_for_function("globalThis.__resolveClipboardRead !== null")
    assert (
        evaluate_string(
            page,
            """() => {
const event = new ClipboardEvent('copy', {bubbles: true, cancelable: true});
document.getElementById('one-time-token').dispatchEvent(event);
return JSON.stringify(event.defaultPrevented);
}""",
        )
        == "true"
    )
    execute_script(page, "() => globalThis.__resolveClipboardRead()")
    expect(page.locator("#login-error")).to_be_visible()
    assert evaluate_string(page, "() => JSON.stringify(globalThis.__clipboardValue)") == '""'
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")

    _issue_token(page, "Clipboard reset client")
    expect(page.locator("#copy-token")).to_be_enabled()
    assert (
        evaluate_string(
            page, "() => JSON.stringify(document.getElementById('one-time-token').tagName)"
        )
        == '"TEXTAREA"'
    )
    expect(page.locator("#one-time-token")).to_have_attribute("readonly", "readonly")
    expect(page.locator("#one-time-token")).not_to_have_attribute("role", "textbox")
    expect(page.locator("#one-time-token")).not_to_have_attribute("aria-readonly", "true")
    expect(page.locator("#one-time-token")).not_to_have_attribute("data-cleanup-locked", "")
    counts = axe_counts(page, _AXE_ASSET)
    assert counts.serious == 0
    assert counts.critical == 0
    assert (
        evaluate_string(
            page,
            "() => JSON.stringify(document.getElementById('one-time-token').inert)",
        )
        == "false"
    )


@pytest.mark.parametrize(
    ("scenario", "title_prefix", "has_action"),
    [
        ("replacement-cooling", "Wait for Key ", False),
        ("replacement-quarantined", "Probe Key ", True),
    ],
)
def test_fresh_session_uses_existing_replacement_before_adding_another(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
    scenario: str,
    title_prefix: str,
    has_action: bool,
) -> None:
    server, _, page = decision_browser
    created = server.state.add_upstream(UpstreamKeyCreateRequest(key=f"synthetic-{scenario}"))
    handle = created.fingerprint.removeprefix("sha256:")[:8]
    install_decision_scenario(page, scenario, server.state)
    _login(page, server)
    _ = page.reload(wait_until="domcontentloaded")
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#decision-title")).to_have_text(
        f"{title_prefix}{handle}" + (" cooldown" if scenario == "replacement-cooling" else "")
    )
    expect(page.locator("#decision-title")).not_to_contain_text("Add replacement")
    if has_action:
        expect(page.locator("#recommended-action")).to_have_text(f"Probe Key {handle}")
    else:
        expect(page.locator("#recommended-action")).to_be_hidden()


def test_confirmed_disable_and_all_revoked_state_do_not_reverse_operator_intent(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    enable_first_key(page)
    page.locator(f"#key-{FIRST_KEY_ID}-toggle").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#last-result")).to_contain_text("was disabled")
    expect(page.locator("#decision-title")).not_to_contain_text("Enable Key aaaaaaaa")

    for token in server.state.tokens().items:
        if token.revoked_at is None:
            server.state.revoke_token(token.id)
    install_decision_scenario(page, "normal", server.state)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#active-token-count")).to_have_text("0")
    expect(page.locator("#decision-title")).to_have_text("No action required")
    expect(page.locator("#recommended-action")).to_be_hidden()
    expect(page.locator("#decision-title")).not_to_contain_text("Issue")


def test_skip_evidence_order_and_ttl_refresh_focus_have_one_natural_path(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
    page.locator(".skip-link").focus()
    expect(page.locator(".skip-link")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#main-content")).to_be_focused()
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
    assert (
        evaluate_string(
            page,
            f"""() => {{
const result = document.getElementById('result-{FIRST_KEY_ID}');
const evidence = document.getElementById('key-{FIRST_KEY_ID}-evidence').closest('details');
const action = document.getElementById('key-{FIRST_KEY_ID}-probe');
return JSON.stringify(Boolean(
  result.compareDocumentPosition(evidence) & Node.DOCUMENT_POSITION_FOLLOWING
  && action.compareDocumentPosition(evidence) & Node.DOCUMENT_POSITION_FOLLOWING
));
}}""",
        )
        == "true"
    )
    install_decision_scenario(page, "short-cooldown", server.state)
    page.locator("#refresh-dashboard").click()
    page.locator("#refresh-dashboard").focus()
    expect(page.locator("#decision-state")).to_contain_text("Stale")
    expect(page.locator("#recommended-action")).to_be_focused()


def test_delete_success_with_refresh_failure_keeps_coherent_last_confirmed_truth(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    page.locator(f"#key-{FIRST_KEY_ID}-delete").click()
    server.state.fail_next("/admin/api/v1/dashboard")
    page.locator("#confirm-action").click()
    expect(page.locator("#global-error")).to_be_visible()
    expect(page.locator("#global-confirmed-result")).to_contain_text("was permanently deleted")
    expect(page.locator("#last-result")).to_contain_text("was permanently deleted")
    expect(page.locator("#gateway-status")).to_contain_text("Stale")
    retained_row = page.locator(f"#result-{FIRST_KEY_ID}").locator("xpath=ancestor::tr")
    expect(retained_row).to_contain_text("Last confirmed")
    page.locator("#retry-dashboard").click()
    expect(page.locator(f"#result-{FIRST_KEY_ID}")).to_have_count(0)
    expect(page.locator("#last-result")).to_contain_text("was permanently deleted")
    expect(page.locator("#upstream-result")).to_be_visible()
    expect(page.locator("#upstream-result")).to_contain_text("was permanently deleted")


def test_delete_success_keeps_its_upstream_location_across_reauthentication(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    consumed = {"value": False}

    def expire_followup_refresh(route: Route) -> None:
        if not consumed["value"]:
            consumed["value"] = True
            route.fulfill(status=401, content_type="application/json", body=_UNAUTHORIZED_BODY)
        else:
            route.continue_()

    _ = page.route("**/admin/api/v1/dashboard", expire_followup_refresh)
    page.locator(f"#key-{FIRST_KEY_ID}-delete").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#login-error")).to_be_visible()
    expect(page.locator("#login-interrupted")).to_contain_text("last confirmed action")
    page.unroute("**/admin/api/v1/dashboard", expire_followup_refresh)
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-result")).to_be_visible()
    expect(page.locator("#upstream-result")).to_contain_text("was permanently deleted")
    expect(page.locator(f"#result-{FIRST_KEY_ID}")).to_have_count(0)


def test_rejected_key_cleanup_beats_unrelated_disabled_key(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _ = server.state.add_upstream(UpstreamKeyCreateRequest(key="synthetic-unrelated-disabled-key"))
    _login(page, server)
    install_decision_scenario(page, "invalid", server.state)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text("Retire rejected Key bbbbbbbb")
    expect(page.locator("#recommended-action")).to_have_text("Review Key bbbbbbbb")
    page.locator("#recommended-action").click()
    expect(page.locator("#key-00000000-0000-4000-8000-000000000002-toggle")).to_be_focused()
    expect(page.locator("#confirm-dialog")).not_to_have_attribute("open", "")


def test_disabled_rejected_key_cannot_enable_or_delete_before_replacement(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    source = next(
        item
        for item in server.state.upstreams().items
        if str(item.id) == "00000000-0000-4000-8000-000000000002"
    )
    _ = server.state.change_upstream(source.id, "disable")
    _login(page, server)
    install_decision_scenario(page, "invalid-only", server.state)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text("Replace Key bbbbbbbb")
    expect(page.locator("#key-00000000-0000-4000-8000-000000000002-enable-reason")).to_contain_text(
        "cannot be enabled"
    )
    expect(page.locator("#key-00000000-0000-4000-8000-000000000002-delete")).to_be_disabled()
    expect(page.locator("#key-00000000-0000-4000-8000-000000000002-delete-reason")).to_contain_text(
        "enable a replacement before deleting"
    )


def test_current_unknown_precedes_previous_confirmed_when_recovery_fails(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    enable_first_key(page)

    def abort_add(route: Route) -> None:
        if route.request.method == "POST":
            route.abort()
        else:
            route.continue_()

    _ = page.route("**/admin/api/v1/upstream-keys", abort_add)
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill("synthetic-priority-unknown")
    page.locator("#submit-upstream").click()
    expect(page.locator("#upstream-error")).to_contain_text("was lost")
    page.locator("[data-close='upstream-dialog']").click()
    page.unroute("**/admin/api/v1/upstream-keys", abort_add)
    _ = page.route("**/admin/api/v1/dashboard", lambda route: route.abort())
    page.locator("#recommended-action").click()
    expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
    expect(page.locator("#global-error-message")).to_contain_text(
        "current operation remains unconfirmed"
    )
    expect(page.locator("#global-error-message")).to_contain_text(
        "Adding an upstream key response was lost"
    )
    expect(page.locator("#global-confirmed-result")).to_contain_text("was enabled")


def test_initial_transport_loss_keeps_previous_confirmed_result_secondary(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    enable_first_key(page)
    probe_path = f"**/admin/api/v1/upstream-keys/{FIRST_KEY_ID}/probe"
    _ = page.route(probe_path, lambda route: route.abort())
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
    expect(page.locator("#global-confirmed-result")).to_contain_text("was enabled")
    expect(page.locator(f"#result-{FIRST_KEY_ID}")).to_contain_text("Success was not assumed")
    expect(page.locator(f"#result-{FIRST_KEY_ID}")).not_to_have_attribute("role", "status")


def test_orphan_recovery_and_current_401_target_are_both_named(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    enable_first_key(page)
    label = "Orphan plus probe client"

    def commit_then_abort(route: Route) -> None:
        _ = route.fetch()
        route.abort()

    _ = page.route("**/admin/api/v1/downstream-tokens", commit_then_abort)
    page.locator("#issue-downstream").click()
    page.locator("#downstream-label").fill(label)
    page.locator("input[name='scope']").first.check()
    page.locator("#submit-downstream").click()
    expect(page.locator("#downstream-error")).to_contain_text("issuance is unknown")
    page.locator("[data-close='downstream-dialog']").click()
    page.unroute("**/admin/api/v1/downstream-tokens", commit_then_abort)
    page.locator("#recommended-action").click()
    expect(page.locator("#decision-title")).to_have_text(f"Review unrecoverable token for {label}")

    path = f"**/admin/api/v1/upstream-keys/{FIRST_KEY_ID}/probe"
    _ = page.route(
        path,
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body=_UNAUTHORIZED_BODY,
        ),
    )
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#login-interrupted")).to_contain_text("Probing Key aaaaaaaa")
    expect(page.locator("#login-interrupted")).to_contain_text(f"Token for {label}")


def test_exact_same_prefix_client_labels_remain_unique_at_l1(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    prefix = ("동일공통Client접두사" * 8)[:60]
    labels = (f"{prefix}-alpha", f"{prefix}-beta")
    for label in labels:
        _ = server.state.issue_token(
            DownstreamTokenIssueRequest(
                label=label,
                scopes=(DownstreamScope.MODELS_READ,),
            )
        )
    page.set_viewport_size({"width": 375, "height": 812})
    _login(page, server)
    for label in labels:
        expect(
            page.locator("#downstream-body .human-label").get_by_text(label, exact=True)
        ).to_have_count(1)
        expect(
            page.get_by_role("button", name=f"Revoke token for {label}", exact=True)
        ).to_have_count(1)
    assert (
        evaluate_string(
            page,
            """() => JSON.stringify(
document.documentElement.scrollWidth <= document.documentElement.clientWidth
)""",
        )
        == "true"
    )


def test_probe_enable_evidence_expires_when_fresh_version_drifts(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#decision-title")).to_have_text("Enable Key aaaaaaaa")

    def drift_version(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        items = tuple(
            item.model_copy(update={"updated_at": datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)})
            if str(item.id) == FIRST_KEY_ID
            else item
            for item in payload.upstream_keys.items
        )
        upstreams = payload.upstream_keys.model_copy(update={"items": items})
        route.fulfill(
            response=response,
            body=_replace_dashboard_upstreams(payload, upstreams).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", drift_version)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#decision-title")).not_to_have_text("Enable Key aaaaaaaa")


def test_deliberate_degraded_disable_does_not_immediately_recommend_probe(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    page.locator(f"#key-{FIRST_KEY_ID}-delete").click()
    page.locator("#confirm-action").click()
    second_id = "00000000-0000-4000-8000-000000000002"
    page.locator(f"#key-{second_id}-toggle").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#last-result")).to_contain_text("was disabled")
    expect(page.locator("#decision-title")).to_have_text("No automatic action")
    expect(page.locator("#decision-title")).not_to_contain_text("Probe Key bbbbbbbb")


def test_open_evidence_survives_refresh_and_persisted_result_is_not_live(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    summary = page.locator(f"#key-{FIRST_KEY_ID}-evidence")
    summary.click()
    expect(summary.locator("xpath=parent::details")).to_have_attribute("open", "")
    page.locator("#refresh-dashboard").click()
    summary = page.locator(f"#key-{FIRST_KEY_ID}-evidence")
    expect(summary.locator("xpath=parent::details")).to_have_attribute("open", "")
    assert (
        evaluate_string(
            page,
            f"""() => JSON.stringify([
  ...document.querySelectorAll('#key-{FIRST_KEY_ID}-evidence + dl dd')
].every((value) => value.classList.contains('machine-id')))""",
        )
        == "true"
    )
    assert (
        evaluate_string(
            page,
            """() => {
const values = [...document.querySelector('#downstream-body details dl').querySelectorAll('dd')];
return JSON.stringify(!values[0].classList.contains('machine-id')
  && values.slice(1).every((value) => value.classList.contains('machine-id')));
}""",
        )
        == "true"
    )
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator(f"#result-{FIRST_KEY_ID}")).to_contain_text("Probe confirmed")
    expect(page.locator(f"#result-{FIRST_KEY_ID}")).not_to_have_attribute("role", "status")
    expect(page.locator("#activity-summary")).to_contain_text("operator probe succeeded")
    expect(page.locator("#activity-summary")).not_to_contain_text("recovery probe")
    assert (
        evaluate_string(
            page,
            """() => JSON.stringify(
[...document.querySelectorAll('#events-body [data-label="Outcome"]')]
  .every((cell) => cell.classList.contains('machine'))
)""",
        )
        == "true"
    )


def test_lost_delete_reconciliation_reports_enabled_prerequisite(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    path = f"**/admin/api/v1/upstream-keys/{FIRST_KEY_ID}"

    def enable_then_abort(route: Route) -> None:
        target = next(
            item.id for item in server.state.upstreams().items if str(item.id) == FIRST_KEY_ID
        )
        _ = server.state.change_upstream(target, "probe")
        _ = server.state.change_upstream(target, "enable")
        route.abort()

    _ = page.route(path, enable_then_abort)
    page.locator(f"#key-{FIRST_KEY_ID}-delete").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#confirm-error")).to_be_visible()
    page.locator("[data-close='confirm-dialog']").click()
    page.unroute(path, enable_then_abort)
    page.locator("#recommended-action").click()
    expect(page.locator("#last-result")).to_contain_text("still exists and is enabled")
    expect(page.locator("#last-result")).to_contain_text("Disable it before trying Delete")


def test_request_evidence_is_safe_and_collapsed_for_each_new_incident(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    for value in ("synthetic-evidence-one", "synthetic-evidence-two"):
        server.state.fail_next("/admin/api/v1/upstream-keys")
        page.locator("#add-upstream").click()
        page.locator("#upstream-key").fill(value)
        page.locator("#submit-upstream").click()
        evidence = page.locator("#upstream-error-evidence")
        expect(evidence).to_be_visible()
        expect(evidence).not_to_have_attribute("open", "")
        expect(evidence.locator(".request-evidence-message")).to_have_text(
            "administration state unavailable"
        )
        expect(evidence.locator("dt")).to_have_text(["Code", "Request"])
        expect(evidence.locator("dd.machine-id")).to_have_count(2)
        expect(evidence.locator("dd.machine-id")).to_have_text(
            ["database_unavailable", "fake-request"]
        )
        evidence.locator("summary").click()
        page.locator("[data-close='upstream-dialog']").click()
        page.locator("#recommended-action").click()

    for index in range(2):
        server.state.fail_next("/admin/api/v1/dashboard")
        page.locator("#refresh-dashboard" if index == 0 else "#retry-dashboard").click()
        evidence = page.locator("#global-error details")
        expect(evidence).to_be_visible()
        expect(evidence).not_to_have_attribute("open", "")
        expect(evidence.locator(".request-evidence-message")).to_have_text(
            "administration state unavailable"
        )
        expect(evidence.locator("dt")).to_have_text(["Code", "Request"])
        expect(evidence.locator("dd.machine-id")).to_have_count(2)
        expect(evidence.locator("dd.machine-id")).to_have_text(
            ["database_unavailable", "fake-request"]
        )
        if index == 0:
            evidence.locator("summary").click()


def test_probe_and_pause_intent_survive_failed_followup_refresh_and_retry(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)

    server.state.fail_next("/admin/api/v1/dashboard")
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Snapshot refresh not confirmed")
    expect(page.locator("#global-confirmed-result")).to_contain_text(
        "Probe confirmed Key aaaaaaaa is valid"
    )
    page.locator("#retry-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text("Enable Key aaaaaaaa")

    toggle = page.locator(f"#key-{FIRST_KEY_ID}-toggle")
    toggle.click()
    expect(toggle).to_have_text("Disable")
    server.state.fail_next("/admin/api/v1/dashboard")
    toggle.click()
    page.locator("#confirm-action").click()
    expect(page.locator("#global-error-state")).to_have_text("Snapshot refresh not confirmed")
    expect(page.locator("#global-confirmed-result")).to_contain_text("was disabled")
    page.locator("#retry-dashboard").click()
    expect(page.locator("#decision-title")).not_to_have_text("Enable Key aaaaaaaa")
    expect(page.locator("#decision-title")).not_to_have_text("Probe Key aaaaaaaa")


def test_unbound_probe_and_pause_evidence_reject_later_fresh_versions(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)

    server.state.fail_next("/admin/api/v1/dashboard")
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Snapshot refresh not confirmed")

    def drift_probe_version(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        items = tuple(
            item.model_copy(update={"updated_at": datetime(2026, 1, 2, tzinfo=UTC)})
            if str(item.id) == FIRST_KEY_ID
            else item
            for item in payload.upstream_keys.items
        )
        upstreams = payload.upstream_keys.model_copy(update={"items": items})
        route.fulfill(
            response=response,
            body=_replace_dashboard_upstreams(payload, upstreams).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", drift_probe_version)
    page.locator("#retry-dashboard").click()
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#decision-title")).not_to_have_text("Enable Key aaaaaaaa")
    page.unroute("**/admin/api/v1/dashboard", drift_probe_version)

    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#decision-title")).to_have_text("Enable Key aaaaaaaa")
    toggle = page.locator(f"#key-{FIRST_KEY_ID}-toggle")
    toggle.click()
    expect(toggle).to_have_text("Disable")
    server.state.fail_next("/admin/api/v1/dashboard")
    toggle.click()
    page.locator("#confirm-action").click()
    expect(page.locator("#global-error-state")).to_have_text("Snapshot refresh not confirmed")

    def drift_paused_version(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        items = tuple(
            item.model_copy(
                update={
                    "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
                    "health_state": HealthState.DEGRADED,
                    "cooldown_until": None,
                    "last_status_class": LastStatusClass.UPSTREAM_UNAVAILABLE,
                }
            )
            if str(item.id) == FIRST_KEY_ID
            else item
            for item in payload.upstream_keys.items
        )
        upstreams = payload.upstream_keys.model_copy(update={"items": items})
        route.fulfill(
            response=response,
            body=_replace_dashboard_upstreams(payload, upstreams).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", drift_paused_version)
    page.locator("#retry-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text("Probe Key aaaaaaaa")


def test_external_version_drift_expires_deliberate_pause_intent(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    page.locator(f"#key-{FIRST_KEY_ID}-delete").click()
    page.locator("#confirm-action").click()
    second_id = "00000000-0000-4000-8000-000000000002"
    page.locator(f"#key-{second_id}-toggle").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#decision-title")).to_have_text("No automatic action")

    def drift_paused_key(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        items = tuple(
            item.model_copy(
                update={
                    "updated_at": datetime(2026, 1, 2, tzinfo=UTC),
                    "cooldown_until": None,
                    "health_state": HealthState.DEGRADED,
                    "last_status_class": LastStatusClass.UPSTREAM_UNAVAILABLE,
                }
            )
            if str(item.id) == second_id
            else item
            for item in payload.upstream_keys.items
        )
        upstreams = payload.upstream_keys.model_copy(update={"items": items})
        route.fulfill(
            response=response,
            body=_replace_dashboard_upstreams(payload, upstreams).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", drift_paused_key)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text("Probe Key bbbbbbbb")


def test_verified_tracked_replacement_wins_then_deliberate_pause_releases_it(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    replacement_value = "synthetic-current-verified-replacement"
    replacement_handle = sha256(replacement_value.encode()).hexdigest()[:8]
    _login(page, server)
    install_decision_scenario(page, "invalid-only", server.state)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text("Replace Key bbbbbbbb")
    page.locator("#recommended-action").click()
    page.locator("#upstream-key").fill(replacement_value)
    page.locator("#submit-upstream").click()
    expect(page.locator("#last-result")).to_contain_text("replacement for Key bbbbbbbb")
    replacement = next(
        item
        for item in server.state.upstreams().items
        if str(item.id)
        not in {
            FIRST_KEY_ID,
            "00000000-0000-4000-8000-000000000002",
        }
    )
    expect(page.locator("#decision-title")).to_have_text(f"Probe Key {replacement_handle}")
    page.locator("#recommended-action").click()
    expect(page.locator("#decision-title")).to_have_text(f"Enable Key {replacement_handle}")

    def drift_replacement_version(route: Route) -> None:
        response = route.fetch()
        dashboard = AdminDashboardRead.model_validate_json(response.text())
        projected = project_upstreams_for_scenario(
            dashboard.upstream_keys,
            "invalid-only",
        )
        items = tuple(
            item.model_copy(update={"updated_at": datetime(2026, 1, 2, tzinfo=UTC)})
            if item.id == replacement.id
            else item
            for item in projected.items
        )
        upstreams = projected.model_copy(update={"items": items})
        route.fulfill(
            response=response,
            body=_replace_dashboard_upstreams(dashboard, upstreams).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", drift_replacement_version)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text(f"Probe Key {replacement_handle}")
    page.unroute("**/admin/api/v1/dashboard", drift_replacement_version)
    page.locator("#recommended-action").click()
    expect(page.locator("#decision-title")).to_have_text(f"Enable Key {replacement_handle}")
    page.locator("#recommended-action").click()

    disable_path = f"**/admin/api/v1/upstream-keys/{replacement.id}/disable"

    def commit_disable_then_abort(route: Route) -> None:
        _ = route.fetch()
        route.abort()

    _ = page.route(disable_path, commit_disable_then_abort)
    page.locator(f"#key-{replacement.id}-toggle").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#confirm-error")).to_contain_text("may have completed")
    expect(page.locator("#confirm-error")).to_contain_text("response was not confirmed")
    page.locator("[data-close='confirm-dialog']").click()
    page.unroute(disable_path, commit_disable_then_abort)
    page.locator("#recommended-action").click()
    expect(page.locator("#last-result")).to_contain_text(
        f"Fresh state confirms Key {replacement_handle} is disabled"
    )
    expect(page.locator("#last-result")).not_to_contain_text("· ID")
    expect(page.locator("#last-result")).to_contain_text(
        "The missing response cannot identify which request disabled it."
    )
    expect(page.locator("#decision-title")).not_to_have_text(f"Probe Key {replacement_handle}")
    expect(page.locator("#decision-title")).not_to_have_text(f"Enable Key {replacement_handle}")
    expect(page.locator("#recommended-action")).not_to_contain_text(f"Key {replacement_handle}")


def test_slow_refresh_preserves_user_owned_navigation_and_evidence_focus(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    held: list[tuple[Route, APIResponse]] = []

    page.locator("#navigation-disclosure summary").click()
    _ = page.route(
        "**/admin/api/v1/dashboard",
        lambda route: _hold_committed_response(held, route),
    )
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#refresh-status")).to_be_focused()
    navigation_target = page.locator("nav a[href='#events']")
    navigation_target.focus()
    route, response = _pop_held_response(page, held)
    route.fulfill(response=response)
    page.unroute("**/admin/api/v1/dashboard")
    expect(navigation_target).to_be_focused()

    evidence = page.locator(f"#key-{FIRST_KEY_ID}-evidence")
    evidence.click()
    _ = page.route(
        "**/admin/api/v1/dashboard",
        lambda route: _hold_committed_response(held, route),
    )
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#refresh-status")).to_be_focused()
    evidence.focus()
    route, response = _pop_held_response(page, held)
    route.fulfill(response=response)
    page.unroute("**/admin/api/v1/dashboard")
    evidence = page.locator(f"#key-{FIRST_KEY_ID}-evidence")
    expect(evidence).to_be_focused()
    expect(evidence.locator("xpath=parent::details")).to_have_attribute("open", "")


def test_refresh_read_delay_does_not_extend_a_known_cooldown_deadline(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    install_decision_scenario(page, "short-cooldown", server.state)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#decision-title")).to_have_text("Wait for Key bbbbbbbb cooldown")
    held: list[tuple[Route, APIResponse, str]] = []
    _ = page.route(
        "**/admin/api/v1/dashboard",
        lambda route: _hold_scenario_dashboard_response(held, "short-cooldown", route),
    )
    page.locator("#refresh-dashboard").click()
    route, response, body = _pop_held_dashboard_response(page, held)
    page.wait_for_timeout(900)
    expect(page.locator("#decision-state")).to_contain_text("Stale")
    expect(page.locator("#recommended-action")).to_have_text("Refresh current state")
    expect(page.locator("#recommended-action")).to_be_disabled()
    expect(page.locator("#generated-at")).to_have_text("Snapshot requires refresh")
    route.fulfill(response=response, body=body)
    page.unroute("**/admin/api/v1/dashboard")
    expect(page.locator("#decision-state")).to_contain_text("Stale")
    expect(page.locator("#recommended-action")).to_have_text("Refresh current state")
    expect(page.locator("#activity-summary")).not_to_contain_text("Just now")
    expect(page.locator("#activity-summary")).to_contain_text("Observed 2026-01-01")


def test_slow_refresh_ages_visible_cooldown_from_the_server_snapshot(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    install_decision_scenario(page, "minute-boundary-cooldown", server.state)
    held: list[tuple[Route, APIResponse, str]] = []
    _ = page.route(
        "**/admin/api/v1/dashboard",
        lambda route: _hold_scenario_dashboard_response(
            held,
            "minute-boundary-cooldown",
            route,
        ),
    )
    page.locator("#refresh-dashboard").click()
    route, response, body = _pop_held_dashboard_response(page, held)
    page.wait_for_timeout(200)
    route.fulfill(response=response, body=body)
    page.unroute("**/admin/api/v1/dashboard")
    row = page.locator("#key-00000000-0000-4000-8000-000000000002-probe").locator(
        "xpath=ancestor::tr"
    )
    expect(row).to_contain_text("Returns automatically in under 1m")
    expect(row).not_to_contain_text("in 2m")


def test_snapshot_expiry_during_probe_keeps_every_refresh_entry_locked(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    install_decision_scenario(page, "short-cooldown", server.state)
    page.locator("#refresh-dashboard").click()
    held: list[tuple[Route, APIResponse]] = []
    probe_path = f"**/admin/api/v1/upstream-keys/{FIRST_KEY_ID}/probe"
    _ = page.route(probe_path, lambda route: _hold_committed_response(held, route))
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    route, response = _pop_held_response(page, held)
    page.wait_for_timeout(900)
    expect(page.locator("#decision-state")).to_contain_text("Stale")
    expect(page.locator("#recommended-action")).to_have_text("Refresh current state")
    expect(page.locator("#recommended-action")).to_be_disabled()
    expect(page.locator("#refresh-dashboard")).to_be_disabled()
    expect(page.locator("#retry-dashboard")).to_be_disabled()
    expect(page.locator("#operation-status")).to_contain_text("Probing Key aaaaaaaa")
    execute_script(
        page,
        """() => {
globalThis.__postProbeRefresh = {started: false, completed: false};
new MutationObserver(() => {
  const busy = document.getElementById("overview").getAttribute("aria-busy");
  if (busy === "true") globalThis.__postProbeRefresh.started = true;
  if (globalThis.__postProbeRefresh.started && busy === "false") {
    globalThis.__postProbeRefresh.completed = true;
  }
}).observe(document.getElementById("overview"), {attributes: true});
}""",
    )
    route.fulfill(response=response)
    expect(page.locator("#last-result")).to_contain_text("Probe confirmed Key aaaaaaaa")
    _ = page.wait_for_function("() => globalThis.__postProbeRefresh.completed")
    page.unroute(probe_path)


def test_reconciliation_result_announces_exactly_once(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    probe_path = f"**/admin/api/v1/upstream-keys/{FIRST_KEY_ID}/probe"

    route_finished = Event()

    def commit_then_abort(route: Route) -> None:
        try:
            _ = route.fetch()
            route.abort()
        finally:
            route_finished.set()

    _ = page.route(probe_path, commit_then_abort)
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
    for _ in range(100):
        if route_finished.is_set():
            break
        page.wait_for_timeout(10)
    assert route_finished.is_set()
    page.unroute(probe_path, commit_then_abort)
    observe_live_nodes(page, ("live-region",))
    page.locator("#retry-dashboard").click()
    expect(page.locator("#last-result")).to_contain_text("Fresh state for Key aaaaaaaa is Verified")
    changes = live_changes(page)
    assert changes.count('"id":"live-region"') == 1
    assert "Fresh state for Key aaaaaaaa is Verified" in changes


def test_add_confirm_and_dismiss_results_each_announce_exactly_once(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    value = "synthetic-single-announcement-add"
    observe_live_nodes(page, ("live-region",))
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill(value)
    page.locator("#submit-upstream").click()
    expect(page.locator("#last-result")).to_contain_text("was encrypted and added disabled")
    expect(page.locator("#live-region")).to_contain_text("was encrypted and added disabled")
    changes = live_changes(page)
    assert changes.count('"id":"live-region"') == 1
    assert "was encrypted and added disabled" in changes

    created = next(
        item
        for item in server.state.upstreams().items
        if str(item.id)
        not in {
            FIRST_KEY_ID,
            "00000000-0000-4000-8000-000000000002",
        }
    )
    page.locator(f"#key-{created.id}-delete").click()
    observe_live_nodes(page, ("live-region",))
    page.locator("#confirm-action").click()
    expect(page.locator("#last-result")).to_contain_text("was permanently deleted")
    expect(page.locator("#live-region")).to_contain_text("was permanently deleted")
    expect(page.locator("#upstream-result")).to_contain_text("was permanently deleted")
    changes = live_changes(page)
    assert changes.count('"id":"live-region"') == 1
    assert "was permanently deleted" in changes

    enable_first_key(page)
    _issue_token(page, "Single dismissal announcement")
    observe_live_nodes(page, ("live-region",))
    page.locator("#dismiss-token").click()
    expect(page.locator("#credential-dialog")).to_be_hidden()
    expect(page.locator("#last-result")).to_contain_text("removed from this page")
    expect(page.locator("#live-region")).to_contain_text("removed from this page")
    changes = live_changes(page)
    assert changes.count('"id":"live-region"') == 1
    assert "removed from this page" in changes


def test_service_offline_recovery_has_one_alert_and_focus_owner(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    probe_path = f"**/admin/api/v1/upstream-keys/{FIRST_KEY_ID}/probe"
    _ = page.route(probe_path, lambda route: route.abort())
    page.locator(f"#key-{FIRST_KEY_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
    page.unroute(probe_path)
    page.locator("#navigation-disclosure summary").click()
    page.locator("#logout").click()
    expect(page.locator("#login-interrupted")).to_contain_text("Probing Key aaaaaaaa")

    _ = page.route(
        "**/admin/api/v1/dashboard",
        lambda route: route.abort(),
        times=1,
    )
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#global-error")).to_be_visible()
    expect(page.locator("#global-error")).to_be_focused()
    expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
    expect(page.locator("#global-error-message")).to_contain_text("Probing Key aaaaaaaa")
    assert page.locator("[role='alert']:visible").count() == 1


def test_lost_add_reconciliation_reports_fresh_enabled_routing_state(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    path = "**/admin/api/v1/upstream-keys"

    def commit_enable_then_abort(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        _ = route.fetch()
        created = server.state.upstreams().items[-1]
        _ = server.state.change_upstream(created.id, "probe")
        _ = server.state.change_upstream(created.id, "enable")
        route.abort()

    _ = page.route(path, commit_enable_then_abort)
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill("synthetic-lost-add-enabled-state")
    page.locator("#submit-upstream").click()
    expect(page.locator("#upstream-error")).to_contain_text("was lost")
    page.locator("[data-close='upstream-dialog']").click()
    page.unroute(path, commit_enable_then_abort)
    page.locator("#recommended-action").click()
    expect(page.locator("#last-result")).to_contain_text("exists, is enabled")
    expect(page.locator("#last-result")).to_contain_text("routing state eligible")


def test_dashboard_events_pair_only_by_started_event_id(
    decision_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = decision_browser
    _login(page, server)
    start_id = UUID("00000000-0000-4000-8000-000000000051")
    terminal_id = UUID("00000000-0000-4000-8000-000000000052")
    unrelated_start_id = UUID("00000000-0000-4000-8000-000000000053")
    legacy_terminal_id = UUID("00000000-0000-4000-8000-000000000054")

    def dashboard(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        fingerprint = payload.upstream_keys.items[0].fingerprint

        def event(
            event_id: UUID,
            outcome: EventOutcome,
            occurred_at: datetime,
            linked_start: UUID | None,
        ) -> AdminDashboardEventRead:
            return AdminDashboardEventRead(
                id=event_id,
                request_id="same-request-and-key",
                event_type=EventType.UPSTREAM_ATTEMPT,
                upstream_key_id=UUID(FIRST_KEY_ID),
                downstream_token_id=None,
                outcome_class=outcome,
                status_class=(LastStatusClass.TIMEOUT if outcome is EventOutcome.FAILED else None),
                latency_ms=25 if outcome is EventOutcome.FAILED else None,
                occurred_at=occurred_at,
                upstream_key_fingerprint=fingerprint,
                attempt_started_event_id=linked_start,
            )

        latest = datetime(2026, 1, 1, 0, 0, 4, tzinfo=UTC)
        events = AdminDashboardEventListResponse(
            items=(
                event(legacy_terminal_id, EventOutcome.FAILED, latest, None),
                event(
                    unrelated_start_id,
                    EventOutcome.STARTED,
                    datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
                    None,
                ),
                event(
                    terminal_id,
                    EventOutcome.FAILED,
                    datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
                    start_id,
                ),
                event(
                    start_id,
                    EventOutcome.STARTED,
                    datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                    None,
                ),
            )
        )
        overview = payload.overview.model_copy(
            update={"last_event_at": latest, "generated_at": latest, "request_count": 2}
        )
        route.fulfill(
            response=response,
            body=payload.model_copy(
                update={"events": events, "overview": overview}
            ).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", dashboard, times=1)
    page.locator("#refresh-dashboard").click()
    summary = page.locator("#activity-summary")
    expect(summary.locator("li")).to_have_count(3)
    page.locator("#audit-events summary").click()
    audit = page.locator("#events-body")
    expect(audit).to_contain_text(f"Exact start {start_id}")
    expect(audit).to_contain_text("Legacy unlinked terminal")
    expect(audit).to_contain_text("No linked terminal in this snapshot window")
