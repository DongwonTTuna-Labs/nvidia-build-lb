"""Focused browser sensors for the judgment-action-evidence product axis."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from threading import Event
from typing import ClassVar
from uuid import UUID

import pytest
from playwright.sync_api import Page, Request, Route, expect
from pydantic import BaseModel, ConfigDict

from nvidia_build_lb.admin.schemas import (
    AdminDashboardEventListResponse,
    AdminDashboardEventRead,
    AdminDashboardRead,
    AdminOverviewRead,
    EventOutcome,
    EventType,
    HealthState,
    LastStatusClass,
    OverviewStatus,
    ReadinessCause,
    RuntimeState,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamRoutingState,
)

from .browser_checks import evaluate_string, execute_script
from .browser_runtime import (
    UI_ORIGIN,
    start_fake_server,
    start_managed_browser,
    stop_fake_server,
    stop_managed_browser,
)
from .fake_admin_state import FakeAdminState

pytestmark = pytest.mark.ui_fake

_FIRST_KEY_ID = "00000000-0000-4000-8000-000000000001"
_SECOND_KEY_ID = "00000000-0000-4000-8000-000000000002"
FIRST_KEY_ID = _FIRST_KEY_ID


class _DecisionGeometry(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    height: float
    title_bottom: float
    action_bottom: float
    primary: int


class _DialogGeometry(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    top: float
    bottom: float
    height: float


def _observe_live_nodes(page: Page, node_ids: tuple[str, ...]) -> None:
    identifiers = "[" + ",".join(f"'{node_id}'" for node_id in node_ids) + "]"
    execute_script(
        page,
        f"""() => {{
for (const observer of globalThis.__liveObservers ?? []) observer.disconnect();
globalThis.__liveChanges = [];
globalThis.__liveObservers = [];
for (const id of {identifiers}) {{
  const node = document.getElementById(id);
  const observer = new MutationObserver(() => {{
    const isLive = node.getAttribute('role') === 'status' || node.hasAttribute('aria-live');
    const text = node.textContent.trim();
    if (isLive && !node.hidden && text) globalThis.__liveChanges.push({{id, text}});
  }});
  observer.observe(node, {{
    childList: true, characterData: true, subtree: true,
    attributes: true, attributeFilter: ['hidden'],
  }});
  globalThis.__liveObservers.push(observer);
}}
}}""",
    )


def _live_changes(page: Page) -> str:
    return evaluate_string(page, "() => JSON.stringify(globalThis.__liveChanges)")


def observe_live_nodes(page: Page, node_ids: tuple[str, ...]) -> None:
    _observe_live_nodes(page, node_ids)


def live_changes(page: Page) -> str:
    return _live_changes(page)


def _enable_first_key(page: Page) -> None:
    page.locator(f"#key-{_FIRST_KEY_ID}-probe").click()
    toggle = page.locator(f"#key-{_FIRST_KEY_ID}-toggle")
    toggle.click()
    expect(toggle).to_have_text("Disable")
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#issue-downstream")).to_be_enabled()


def enable_first_key(page: Page) -> None:
    _enable_first_key(page)


def _refresh_dashboard_from(page: Page, control_id: str) -> None:
    with page.expect_response(lambda response: response.url.endswith("/dashboard")) as refresh:
        page.locator(control_id).click()
    assert refresh.value.status == 200
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")


def _open_admin_with_paused_clock(page: Page, admin_bearer: str) -> None:
    page.clock.install()
    _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
    page.clock.pause_at(float(evaluate_string(page, "() => String(Date.now())")))
    page.locator("#admin-bearer").fill(admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")


def _start_reviewed_row_action(page: Page, control_id: str) -> None:
    page.locator("#recommended-action").click()
    control = page.locator(f"#{control_id}")
    expect(control).to_be_focused()
    expect(page.locator("#confirm-dialog")).not_to_have_attribute("open", "")
    control.click()


def _commit_revoke_then_abort(
    route: Route,
    *,
    state: FakeAdminState,
    label: str,
    finished: Event,
) -> None:
    if route.request.method != "POST":
        route.continue_()
        return
    try:
        _ = route.fetch()
        created = next((item for item in state.tokens().items if item.label == label), None)
        if created is None:
            message = "committed token was not observable in the fake repository"
            raise AssertionError(message)
        state.revoke_token(created.id)
        route.abort()
    finally:
        finished.set()


def _project_invalid_only(payload: UpstreamKeyListResponse) -> UpstreamKeyListResponse:
    items = list(payload.items)
    replacement_present = any(str(item.id) not in {_FIRST_KEY_ID, _SECOND_KEY_ID} for item in items)
    if not replacement_present:
        items = [item for item in items if str(item.id) != _FIRST_KEY_ID]
    source_index = next(
        (index for index, item in enumerate(items) if str(item.id) == _SECOND_KEY_ID),
        None,
    )
    if source_index is None:
        return payload.model_copy(update={"items": tuple(items)})
    source = items[source_index]
    items[source_index] = source.model_copy(
        update={
            "routing_state": (
                UpstreamRoutingState.QUARANTINED
                if source.enabled
                else UpstreamRoutingState.DISABLED
            ),
            "health_state": HealthState.DEGRADED,
            "last_status_class": LastStatusClass.INVALID_CREDENTIAL,
        }
    )
    return payload.model_copy(update={"items": tuple(items)})


def _project_upstreams(
    payload: UpstreamKeyListResponse,
    key_scenario: str,
) -> UpstreamKeyListResponse:
    if key_scenario == "invalid-only":
        return _project_invalid_only(payload)
    if key_scenario in {"replacement-cooling", "replacement-quarantined"}:
        replacement = next(
            item for item in payload.items if str(item.id) not in {_FIRST_KEY_ID, _SECOND_KEY_ID}
        )
        source = next(item for item in payload.items if str(item.id) == _SECOND_KEY_ID)
        source = source.model_copy(
            update={
                "enabled": False,
                "routing_state": UpstreamRoutingState.DISABLED,
                "health_state": HealthState.DEGRADED,
                "last_status_class": LastStatusClass.INVALID_CREDENTIAL,
            }
        )
        replacement = replacement.model_copy(
            update={
                "enabled": key_scenario == "replacement-cooling",
                "routing_state": (
                    UpstreamRoutingState.COOLDOWN
                    if key_scenario == "replacement-cooling"
                    else UpstreamRoutingState.QUARANTINED
                ),
                "health_state": HealthState.DEGRADED,
                "cooldown_until": (
                    datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
                    if key_scenario == "replacement-cooling"
                    else None
                ),
                "last_status_class": (
                    LastStatusClass.RATE_LIMITED
                    if key_scenario == "replacement-cooling"
                    else LastStatusClass.UPSTREAM_UNAVAILABLE
                ),
            }
        )
        return payload.model_copy(update={"items": (source, replacement)})
    items = list(payload.items)
    first_index = next(index for index, item in enumerate(items) if str(item.id) == _FIRST_KEY_ID)
    items[first_index] = items[first_index].model_copy(
        update={
            "enabled": True,
            "routing_state": UpstreamRoutingState.ELIGIBLE,
            "health_state": HealthState.HEALTHY,
            "cooldown_until": None,
            "last_status_class": LastStatusClass.SUCCESS,
        }
    )
    second_index = next(
        (index for index, item in enumerate(items) if str(item.id) == _SECOND_KEY_ID),
        None,
    )
    if second_index is None:
        return payload.model_copy(update={"items": tuple(items)})
    original_second = items[second_index]
    second = original_second.model_copy(
        update={
            "enabled": True,
            "routing_state": UpstreamRoutingState.ELIGIBLE,
            "health_state": HealthState.HEALTHY,
            "cooldown_until": None,
            "last_status_class": LastStatusClass.SUCCESS,
        }
    )
    if key_scenario in {"quarantined", "invalid", "credits"}:
        status_by_scenario = {
            "quarantined": LastStatusClass.UPSTREAM_UNAVAILABLE,
            "invalid": LastStatusClass.INVALID_CREDENTIAL,
            "credits": LastStatusClass.CREDITS_EXHAUSTED,
        }
        second = second.model_copy(
            update={
                "enabled": original_second.enabled,
                "routing_state": (
                    UpstreamRoutingState.QUARANTINED
                    if original_second.enabled
                    else UpstreamRoutingState.DISABLED
                ),
                "health_state": HealthState.DEGRADED,
                "last_status_class": status_by_scenario[key_scenario],
            }
        )
    elif key_scenario == "cooldown":
        second = second.model_copy(
            update={
                "routing_state": UpstreamRoutingState.COOLDOWN,
                "health_state": HealthState.DEGRADED,
                "cooldown_until": datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
                "last_status_class": LastStatusClass.RATE_LIMITED,
            }
        )
    elif key_scenario in {"short-cooldown", "minute-boundary-cooldown"}:
        delay = 800 if key_scenario == "short-cooldown" else 60_100
        second = second.model_copy(
            update={
                "routing_state": UpstreamRoutingState.COOLDOWN,
                "health_state": HealthState.DEGRADED,
                "cooldown_until": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(milliseconds=delay),
                "last_status_class": LastStatusClass.RATE_LIMITED,
            }
        )
    elif key_scenario in {"ended-cooldown", "disabled-ended-cooldown"}:
        enabled = key_scenario == "ended-cooldown"
        second = second.model_copy(
            update={
                "enabled": enabled,
                "routing_state": (
                    UpstreamRoutingState.ELIGIBLE if enabled else UpstreamRoutingState.DISABLED
                ),
                "health_state": HealthState.DEGRADED,
                "cooldown_until": datetime(2025, 12, 31, 23, 59, tzinfo=UTC),
                "last_status_class": LastStatusClass.RATE_LIMITED,
            }
        )
    elif key_scenario == "disabled-cooldown":
        second = second.model_copy(
            update={
                "enabled": False,
                "routing_state": UpstreamRoutingState.COOLDOWN,
                "health_state": HealthState.DEGRADED,
                "cooldown_until": datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
                "last_status_class": LastStatusClass.RATE_LIMITED,
            }
        )
    elif key_scenario == "disabled":
        second = second.model_copy(
            update={"enabled": False, "routing_state": UpstreamRoutingState.DISABLED}
        )
    items[second_index] = second
    return payload.model_copy(update={"items": tuple(items)})


def project_upstreams_for_scenario(
    payload: UpstreamKeyListResponse,
    key_scenario: str,
) -> UpstreamKeyListResponse:
    return _project_upstreams(payload, key_scenario)


def _project_overview(
    payload: AdminOverviewRead,
    key_scenario: str,
    *,
    no_client: bool,
    upstreams: UpstreamKeyListResponse,
) -> AdminOverviewRead:
    projected = _project_upstreams(upstreams, key_scenario).items
    eligible = sum(item.routing_state is UpstreamRoutingState.ELIGIBLE for item in projected)
    upstream_keys = payload.upstream_keys.model_copy(
        update={
            "total": len(projected),
            "enabled": sum(item.enabled for item in projected),
            "eligible": eligible,
            "cooling": sum(
                item.routing_state is UpstreamRoutingState.COOLDOWN for item in projected
            ),
            "degraded": sum(item.health_state is HealthState.DEGRADED for item in projected),
        }
    )
    downstream_tokens = payload.downstream_tokens
    if no_client:
        downstream_tokens = downstream_tokens.model_copy(
            update={"total": 0, "active": 0, "revoked": 0}
        )
    return payload.model_copy(
        update={
            "ready": eligible > 0,
            "status": OverviewStatus.OK if eligible > 0 else OverviewStatus.DEGRADED,
            "upstream_keys": upstream_keys,
            "downstream_tokens": downstream_tokens,
        }
    )


def _install_decision_scenario(page: Page, scenario: str, state: FakeAdminState) -> None:
    key_scenario = scenario.removesuffix("-no-client")
    no_client = scenario.endswith("-no-client")

    def dashboard(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        upstreams = _project_upstreams(state.upstreams(), key_scenario)
        downstreams = (
            payload.downstream_tokens.model_copy(update={"items": ()})
            if no_client
            else payload.downstream_tokens
        )
        overview = _project_overview(
            payload.overview,
            key_scenario,
            no_client=no_client,
            upstreams=state.upstreams(),
        )
        readiness = ReadinessCause.READY if overview.ready else ReadinessCause.NO_ELIGIBLE_UPSTREAM
        route.fulfill(
            response=response,
            body=payload.model_copy(
                update={
                    "readiness_cause": readiness,
                    "overview": overview,
                    "upstream_keys": upstreams,
                    "downstream_tokens": downstreams,
                }
            ).model_dump_json(),
        )

    _ = page.route("**/admin/api/v1/dashboard", dashboard)


def install_decision_scenario(page: Page, scenario: str, state: FakeAdminState) -> None:
    _install_decision_scenario(page, scenario, state)


def _assert_initial_decision(page: Page) -> None:
    expect(page.locator("#decision-title")).to_have_text("Probe Key aaaaaaaa")
    expect(page.locator("#recommended-action")).to_have_text("Probe Key aaaaaaaa")
    toggle = page.locator(f"#key-{_FIRST_KEY_ID}-toggle")
    expect(toggle).to_be_disabled()
    expect(toggle).to_have_attribute(
        "aria-describedby",
        f"key-{_FIRST_KEY_ID}-enable-reason",
    )
    expect(page.locator(f"#key-{_FIRST_KEY_ID}-probe")).to_have_attribute(
        "aria-label",
        "Probe Key aaaaaaaa",
    )
    visible_events = page.locator("#events").inner_text()
    assert _FIRST_KEY_ID not in visible_events
    assert "2026-01-01T00:00:00Z" not in visible_events
    expect(page.locator("summary[aria-label='Evidence for Key aaaaaaaa']")).to_have_count(1)


def _complete_first_key(page: Page) -> None:
    toggle = page.locator(f"#key-{_FIRST_KEY_ID}-toggle")
    page.locator("#recommended-action").click()
    expect(page.locator("#decision-title")).to_have_text("Enable Key aaaaaaaa")
    expect(toggle).to_be_enabled()
    expect(page.locator("#last-result")).to_contain_text("Probe confirmed Key aaaaaaaa is valid")
    page.locator("#recommended-action").click()
    expect(page.locator("#decision-title")).to_have_text("Wait for Key bbbbbbbb cooldown")
    expect(page.locator("#recommended-action")).to_be_hidden()
    expect(page.locator("#last-result")).to_contain_text("Key aaaaaaaa was enabled")


def _assert_progressive_evidence(page: Page) -> None:
    audit = page.locator("#audit-events")
    expect(audit).not_to_have_attribute("open", "")
    audit.locator("summary").click()
    expect(audit).to_contain_text("Key aaaaaaaa")
    expect(audit).to_contain_text("Exact start")
    expect(audit).to_contain_text("2026-01-01T00:00:00Z")
    audit.locator("summary").click()


def _exercise_stale_lock(page: Page, abort_dashboard: Callable[[Route], None]) -> None:
    _ = page.route("**/admin/api/v1/dashboard", abort_dashboard)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#global-error")).to_be_focused()
    expect(page.locator("#decision-brief")).to_be_hidden()
    expect(page.locator("#decision-title")).to_have_text("Refresh before making changes")
    expect(page.locator("#recommended-action")).to_be_hidden()
    expect(page.locator("#retry-dashboard")).to_be_visible()
    expect(page.locator("#retry-dashboard")).to_be_enabled()
    expect(page.locator("#gateway-status")).to_have_text("Stale · Readiness requires refresh")
    expect(page.locator("#eligible-count")).to_contain_text("Last confirmed")
    expect(page.locator("#request-count")).to_contain_text("Last confirmed")
    expect(page.locator("#last-event-at")).to_have_text(
        "Stale · Last event freshness requires refresh"
    )
    expect(page.locator("#issue-downstream-reason")).to_have_text(
        "Current state is unknown. Refresh before issuing a downstream token."
    )
    assert page.get_by_role("button", name="Refresh current state", exact=True).count() == 1
    assert page.locator("[data-mutation]:enabled").count() == 0
    page.unroute("**/admin/api/v1/dashboard", abort_dashboard)
    page.locator("#retry-dashboard").click()
    expect(page.locator("#decision-brief")).to_be_visible()
    expect(page.locator("#decision-title")).to_have_text("Wait for Key bbbbbbbb cooldown")


def _exercise_linked_form_errors(page: Page) -> None:
    page.locator("#add-upstream").click()
    page.locator("#submit-upstream").click()
    expect(page.locator("#upstream-key")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#upstream-error")).to_contain_text("Enter a non-empty")
    page.locator("[data-close='upstream-dialog']").click()
    page.locator("#issue-downstream").click()
    page.locator("#submit-downstream").click()
    expect(page.locator("#downstream-label")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#downstream-scopes")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#downstream-error")).to_contain_text("choose at least one scope")
    execute_script(
        page,
        """() => {
const target = document.getElementById('downstream-error');
globalThis.__downstreamAlertChanges = 0;
new MutationObserver((records) => { globalThis.__downstreamAlertChanges += records.length; })
  .observe(target, {childList:true, characterData:true, subtree:true});
}""",
    )
    page.locator("#downstream-label").press_sequentially("Independent-validation")
    alert_changes = evaluate_string(
        page, "() => JSON.stringify(globalThis.__downstreamAlertChanges)"
    )
    assert int(alert_changes) == 1
    expect(page.locator("#downstream-label")).not_to_have_attribute("aria-invalid", "true")
    expect(page.locator("#downstream-scopes")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#downstream-error")).to_be_visible()
    page.locator("#submit-downstream").click()
    first_scope = page.locator("input[name='scope']").first
    expect(first_scope).to_be_focused()
    expect(first_scope).to_have_attribute("aria-describedby", "scope-help downstream-error")
    first_scope.check()
    expect(page.locator("#downstream-error")).to_be_hidden()


def test_narrow_operator_flow_prioritizes_one_action_and_locks_stale_mutations() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 375, "height": 812})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def abort_dashboard(route: Route) -> None:
        route.abort()

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        navigation = page.locator("#navigation-disclosure")
        expect(navigation).to_be_hidden()
        expect(navigation).not_to_have_attribute("open", "")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        page.locator("#dashboard-title").wait_for(state="visible")

        expect(navigation).to_be_visible()
        expect(navigation).not_to_have_attribute("open", "")
        navigation.locator("summary").click()
        page.get_by_role("link", name="Upstream keys", exact=True).click()
        expect(page.locator("#upstream-heading")).to_be_focused()
        expect(page.locator("nav a[href='#upstream-keys']")).to_have_attribute(
            "aria-current", "location"
        )
        _assert_initial_decision(page)
        _complete_first_key(page)
        _assert_progressive_evidence(page)
        _exercise_stale_lock(page, abort_dashboard)
        _exercise_linked_form_errors(page)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_showcase_navigation_is_open_to_ax_and_tracks_the_selected_section() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/showcase", wait_until="load")
        disclosure = page.locator(".navigation-disclosure")
        expect(disclosure).to_have_attribute("open", "")
        navigation_snapshot = page.locator("nav").aria_snapshot()
        assert 'navigation "Showcase sections"' in navigation_snapshot
        for label in ("Buttons", "Inputs", "Statuses", "Tables", "Dialogs", "System states"):
            assert f'link "{label}"' in navigation_snapshot

        page.keyboard.press("Tab")
        expect(page.locator(".skip-link")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.locator("#main-content")).to_be_focused()
        expect(page.locator("nav a[href='#buttons']")).to_have_attribute("aria-current", "location")
        page.locator(".skip-link").focus()
        page.keyboard.press("Tab")
        expect(page.locator("nav a[href='#buttons']")).to_be_focused()

        page.locator("nav a[href='#inputs']").click()
        expect(page.locator("#input-heading")).to_be_focused()
        expect(page.locator("nav a[href='#inputs']")).to_have_attribute("aria-current", "location")
        expect(page.locator("nav a[href='#buttons']")).not_to_have_attribute(
            "aria-current", "location"
        )

        page.locator("nav a[href='#inputs']").focus()
        page.set_viewport_size({"width": 640, "height": 900})
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator("summary")).to_be_focused()
        disclosure.locator("summary").click()
        page.locator("nav a[href='#system-states']").click()
        expect(disclosure).not_to_have_attribute("open", "")
        expect(page.locator("#system-states-heading")).to_be_focused()
        expect(page.locator("nav a[href='#system-states']")).to_have_attribute(
            "aria-current", "location"
        )
        disclosure.locator("summary").focus()
        page.set_viewport_size({"width": 1280, "height": 900})
        expect(page.locator("nav a[href='#system-states']")).to_be_focused()
        _ = page.go_back()
        expect(page.locator("#input-heading")).to_be_focused()
        expect(page.locator("nav a[href='#inputs']")).to_have_attribute("aria-current", "location")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_authenticated_reflow_closes_navigation_and_refresh_announces_once_per_phase() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="load")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        disclosure = page.locator("#navigation-disclosure")
        expect(disclosure).to_have_attribute("open", "")

        page.locator("nav a[href='#events']").focus()
        page.set_viewport_size({"width": 640, "height": 900})
        expect(disclosure).not_to_have_attribute("open", "")
        expect(disclosure.locator("summary")).to_be_focused()
        expect(page.locator("#activity-summary")).not_to_have_attribute("role", "status")
        _observe_live_nodes(
            page,
            ("refresh-status", "live-region", "activity-summary"),
        )
        page.locator("#refresh-dashboard").click()
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        changes = _live_changes(page)
        assert changes == (
            '[{"id":"refresh-status","text":"Refreshing current administration state…"},'
            '{"id":"live-region","text":"Current administration state refreshed."}]'
        )
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_login_and_refresh_each_read_one_dashboard_without_legacy_fallback() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        server.state.reset_network_audit()
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        login_api = tuple(
            (item.method, item.path, item.status)
            for item in server.state.network_audit()
            if item.path.startswith("/admin/api/")
        )
        assert login_api == (("GET", "/admin/api/v1/dashboard", 200),)

        server.state.reset_network_audit()
        page.locator("#refresh-dashboard").click()
        expect(page.locator("#refresh-dashboard")).to_have_attribute("aria-busy", "false")
        refresh_api = tuple(
            (item.method, item.path, item.status)
            for item in server.state.network_audit()
            if item.path.startswith("/admin/api/")
        )
        assert refresh_api == (("GET", "/admin/api/v1/dashboard", 200),)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_dashboard_404_is_incompatible_without_legacy_read_fallback() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    requested_api: list[str] = []

    def observe_request(request: Request) -> None:
        if "/admin/api/" in request.url:
            requested_api.append(request.url)

    def missing_dashboard(route: Route) -> None:
        route.fulfill(status=404, content_type="application/json", body="")

    try:
        page.on("request", observe_request)
        _ = page.route("**/admin/api/v1/dashboard", missing_dashboard, times=1)
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#global-error-state")).to_have_text("Incompatible service")
        expect(page.locator("#global-error-message")).to_contain_text(
            "legacy reads are not used as a fallback"
        )
        assert requested_api == [f"{UI_ORIGIN}/admin/api/v1/dashboard"]

        page.locator("#retry-dashboard").click()
        expect(page.locator("#global-error")).to_be_hidden()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_initial_snapshot_skew_stays_unavailable_until_all_reads_agree() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def skew_dashboard(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        upstream_keys = payload.overview.upstream_keys.model_copy(
            update={"total": payload.overview.upstream_keys.total + 1}
        )
        overview = payload.overview.model_copy(update={"upstream_keys": upstream_keys})
        body = payload.model_copy(update={"overview": overview}).model_dump_json()
        route.fulfill(response=response, body=body)

    try:
        _ = page.route("**/admin/api/v1/dashboard", skew_dashboard)
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#decision-state")).to_contain_text("Unavailable")
        expect(page.locator("#retry-dashboard")).to_be_visible()
        expect(page.locator("#recommended-action")).to_be_hidden()
        expect(page.locator("#refresh-dashboard")).to_be_hidden()
        expect(page.locator("#upstream-body")).to_contain_text("Unavailable")
        expect(page.locator("#downstream-body")).to_contain_text("Unavailable")
        expect(page.locator("#activity-summary")).to_contain_text("unavailable")
        expect(page.locator("#audit-summary")).to_have_text("Audit details · unavailable")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        expect(page.locator("#global-error-message")).to_contain_text("did not complete")
        assert page.locator("[data-mutation]:enabled").count() == 0
        page.unroute("**/admin/api/v1/dashboard", skew_dashboard)
        page.locator("#retry-dashboard").click()
        expect(page.locator("#global-error")).to_be_hidden()
        expect(page.locator("#decision-state")).not_to_contain_text("Unavailable")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_open_dialog_explains_snapshot_expiry_and_preserves_input_until_cancel() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def abort_dashboard(route: Route) -> None:
        route.abort()

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        page.locator("#add-upstream").click()
        page.locator("#upstream-key").fill("synthetic-preserved-until-cancel")
        _ = page.route("**/admin/api/v1/dashboard", abort_dashboard)
        page.evaluate("document.querySelector('#refresh-dashboard').click()")
        expect(page.locator("#upstream-dialog-stale")).to_be_visible()
        expect(page.locator("#upstream-dialog-stale")).to_contain_text(
            "Cancel this dialog, then refresh before deciding what to do next."
        )
        expect(page.locator("#submit-upstream")).to_be_disabled()
        expect(page.locator("[data-close='upstream-dialog']")).to_be_enabled()
        expect(page.locator("#upstream-key")).to_have_value("synthetic-preserved-until-cancel")
        expect(page.locator("#upstream-dialog")).to_have_attribute(
            "aria-describedby", "upstream-description upstream-dialog-stale"
        )
        page.locator("[data-close='upstream-dialog']").click()
        expect(page.locator("#retry-dashboard")).to_be_visible()
        page.unroute("**/admin/api/v1/dashboard", abort_dashboard)
        page.locator("#retry-dashboard").click()
        page.locator("#add-upstream").click()
        expect(page.locator("#upstream-dialog")).to_have_attribute(
            "aria-describedby", "upstream-description"
        )
        expect(page.locator("#upstream-dialog-stale")).to_be_hidden()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_snapshot_expiry_keeps_focus_in_modal_and_uses_a_connected_cancel_fallback() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _open_admin_with_paused_clock(page, server.state.admin_bearer)
        _install_decision_scenario(page, "short-cooldown", server.state)
        _refresh_dashboard_from(page, "#refresh-dashboard")

        evidence = page.locator(f"#key-{_FIRST_KEY_ID}-evidence")
        evidence.click()
        expect(evidence).to_be_focused()
        page.clock.run_for(800)
        expect(page.locator("#decision-state")).to_contain_text("Stale")
        expect(evidence).to_be_focused()
        expect(evidence.locator("xpath=parent::details")).to_have_attribute("open", "")
        _refresh_dashboard_from(page, "#recommended-action")

        page.locator("#add-upstream").click()
        page.locator("#upstream-key").fill("synthetic-modal-expiry-focus")
        _observe_live_nodes(page, ("upstream-dialog-stale", "live-region"))
        page.locator("#submit-upstream").focus()
        page.clock.run_for(800)
        expect(page.locator("#upstream-dialog-stale")).to_be_focused()
        expect(page.locator("#upstream-dialog")).to_be_visible()
        assert '"id":"upstream-dialog-stale"' in _live_changes(page)
        assert '"id":"live-region"' not in _live_changes(page)
        page.locator("[data-close='upstream-dialog']").click()
        expect(page.locator("#dashboard-title")).to_be_focused()

        _refresh_dashboard_from(page, "#recommended-action")
        first_toggle = page.locator(f"#key-{_FIRST_KEY_ID}-toggle")
        expect(first_toggle).to_have_text("Disable")
        first_toggle.click()
        page.locator("#confirm-action").focus()
        page.clock.run_for(800)
        expect(page.locator("#confirm-dialog-stale")).to_be_focused()
        expect(page.locator("#confirm-dialog")).to_be_visible()
        page.locator("[data-close='confirm-dialog']").click()
        expect(page.locator("#upstream-heading")).to_be_focused()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def _exercise_malformed_probe(
    page: Page,
    malformed_success: Callable[[Route], None],
) -> None:
    probe_path = f"**/admin/api/v1/upstream-keys/{_FIRST_KEY_ID}/probe"
    _ = page.route(probe_path, malformed_success)
    page.locator(f"#key-{_FIRST_KEY_ID}-probe").click()
    expect(page.locator("#global-error-message")).to_contain_text("Invalid response")
    expect(page.locator("#global-error-message")).not_to_contain_text("Offline")
    expect(page.locator(f"#result-{_FIRST_KEY_ID}")).to_contain_text("invalid service response")
    page.unroute(probe_path, malformed_success)
    page.locator("#retry-dashboard").click()
    expect(page.locator(f"#key-{_FIRST_KEY_ID}-probe")).to_be_focused()


def _exercise_malformed_add(
    page: Page,
    malformed_success: Callable[[Route], None],
) -> None:
    add_path = "**/admin/api/v1/upstream-keys"
    _ = page.route(add_path, malformed_success)
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill("synthetic-malformed-add")
    page.locator("#submit-upstream").click()
    upstream_error = page.locator("#upstream-error")
    expect(upstream_error).to_contain_text("invalid response")
    expect(upstream_error).not_to_contain_text("response was lost")
    expect(upstream_error).not_to_contain_text("offline")
    expect(page.locator("#upstream-key")).not_to_have_attribute("aria-invalid", "true")
    page.locator("[data-close='upstream-dialog']").click()
    page.unroute(add_path, malformed_success)
    page.locator("#recommended-action").click()


def _exercise_malformed_issue(
    page: Page,
    malformed_success: Callable[[Route], None],
) -> None:
    issue_path = "**/admin/api/v1/downstream-tokens"
    _ = page.route(issue_path, malformed_success)
    page.locator("#issue-downstream").click()
    page.locator("#downstream-label").fill("Malformed issue client")
    page.locator("input[name='scope']").first.check()
    page.locator("#submit-downstream").click()
    downstream_error = page.locator("#downstream-error")
    expect(downstream_error).to_contain_text("invalid response")
    expect(downstream_error).not_to_contain_text("response was lost")
    expect(downstream_error).not_to_contain_text("offline")
    expect(page.locator("#downstream-label")).not_to_have_attribute("aria-invalid", "true")
    expect(page.locator("#downstream-scopes")).not_to_have_attribute("aria-invalid", "true")


def test_malformed_mutation_successes_are_invalid_responses_not_transport_failures() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def malformed_success(route: Route) -> None:
        if route.request.method == "GET":
            route.continue_()
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        _enable_first_key(page)
        _exercise_malformed_probe(page, malformed_success)
        _exercise_malformed_add(page, malformed_success)
        _exercise_malformed_issue(page, malformed_success)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_one_time_credential_names_client_and_access_then_clears_target() -> None:  # noqa: PLR0915
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 375, "height": 812})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    page = context.new_page()
    page.set_default_timeout(5_000)
    label = ("긴Client" * 22)[:128]
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        _enable_first_key(page)
        page.locator(f"#key-{_FIRST_KEY_ID}-probe").click()
        expect(page.locator("#last-result")).to_contain_text("remains enabled")
        expect(page.locator("#last-result")).not_to_contain_text("Enable is now available")
        page.locator("#issue-downstream").click()
        page.locator("#downstream-label").fill(label)
        for scope in page.locator("input[name='scope']").all():
            scope.check()
        _observe_live_nodes(page, ("downstream-dialog-busy", "live-region"))
        page.locator("#submit-downstream").click()

        expect(page.locator("#credential-dialog")).to_be_visible()
        issued = next(item for item in server.state.tokens().items if item.label == label)
        expect(page.locator("#credential-title")).to_have_text(
            f"Store credential for {label[:48]}…"
        )
        expect(page.locator("#credential-target")).to_have_text(
            f"Client {label} · Read models · Write chat"
        )
        expect(page.locator("#credential-id")).to_have_value(str(issued.id))
        expect(page.locator("#credential-title")).to_be_focused()
        assert '"id":"downstream-dialog-busy"' in _live_changes(page)
        assert '"id":"live-region"' not in _live_changes(page)
        assert (
            evaluate_string(
                page,
                """() => JSON.stringify(
document.documentElement.scrollWidth <= document.documentElement.clientWidth
)""",
            )
            == "true"
        )
        _observe_live_nodes(
            page,
            ("credential-busy", "credential-status", "live-region"),
        )
        page.locator("#copy-token").click()
        expect(page.locator("#copy-token")).to_have_text("Copy again")
        copy_changes = _live_changes(page)
        assert '"id":"credential-busy"' in copy_changes
        assert '"id":"credential-status"' in copy_changes
        assert '"id":"live-region"' not in copy_changes
        page.locator("#dismiss-token").click()
        expect(page.locator("#credential-dialog")).to_be_hidden()
        expect(page.locator("#credential-target")).to_be_empty()
        expect(page.locator("#credential-id")).to_have_value("")
        expect(page.locator("#credential-title")).to_have_text("Store this credential now")
        expect(page.locator("#one-time-token")).to_be_empty()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def _logout_and_reauthenticate_orphan_recovery(page: Page, state: FakeAdminState) -> None:
    page.locator("#navigation-disclosure summary").click()
    page.locator("nav a[href='#events']").click()
    expect(page.locator("nav a[href='#events']")).to_have_attribute("aria-current", "location")
    page.locator("#navigation-disclosure summary").click()
    page.locator("#logout").click()
    expect(page.locator("#login-interrupted")).to_contain_text(
        "one-time credential was not received"
    )
    page.locator("#admin-bearer").fill("synthetic-wrong-admin-bearer")
    expect(page.locator("#login-interrupted")).to_be_visible()
    page.keyboard.press("Enter")
    expect(page.locator("#login-error")).to_be_visible()
    expect(page.locator("#login-error-message")).to_have_text(
        "Authentication expired or failed. Enter the current admin bearer."
    )
    expect(page.locator("#admin-bearer")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#login-submit")).to_be_enabled()

    def abort_dashboard(route: Route) -> None:
        route.abort()

    _ = page.route("**/admin/api/v1/dashboard", abort_dashboard)
    page.locator("#admin-bearer").fill(state.admin_bearer)
    expect(page.locator("#login-interrupted")).to_be_visible()
    page.keyboard.press("Enter")
    expect(page.locator("#global-error")).to_be_visible()
    expect(page.locator("#global-confirmed-result")).to_contain_text(
        "one-time credential response was lost"
    )
    expect(page.locator("#retry-dashboard")).to_be_enabled()
    page.unroute("**/admin/api/v1/dashboard", abort_dashboard)
    page.locator("#retry-dashboard").click()
    expect(page.locator("nav a[href='#overview']")).to_have_attribute("aria-current", "location")
    expect(page.locator("nav a[href='#events']")).not_to_have_attribute("aria-current", "location")
    assert evaluate_string(page, "() => JSON.stringify(window.location.hash)") == '"#overview"'


def _reconcile_externally_revoked_orphan(
    page: Page,
    state: FakeAdminState,
    label: str,
) -> None:
    orphan = next(item for item in state.tokens().items if item.label == label)
    state.revoke_token(orphan.id)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#last-result")).to_contain_text(
        f"Fresh state confirms the token for {label} is revoked"
    )
    expect(page.locator("#last-result")).not_to_contain_text("replacement may")
    expect(page.locator("#decision-title")).not_to_contain_text("Revoke token")
    expect(page.locator("#issue-downstream")).to_be_enabled()


def test_lost_issue_response_reconciles_to_revoke_before_replacement() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    label = "Lost response client"

    def commit_then_abort(route: Route) -> None:
        _ = route.fetch()
        route.abort()

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        _enable_first_key(page)
        _ = page.route("**/admin/api/v1/downstream-tokens", commit_then_abort)
        page.locator("#issue-downstream").click()
        page.locator("#downstream-label").fill(label)
        page.locator("input[name='scope']").first.check()
        page.locator("#submit-downstream").click()
        expect(page.locator("#downstream-error")).to_contain_text("issuance is unknown")
        expect(page.locator("#downstream-error")).to_contain_text(f"Client {label} · Read models")
        expect(page.locator("#downstream-error")).to_contain_text("editable fields are a new draft")
        expect(page.locator("#downstream-error")).to_be_focused()
        expect(page.locator("#downstream-dialog-stale")).to_be_hidden()
        page.locator("#downstream-label").press("End")
        page.locator("#downstream-label").press_sequentially(" changed")
        expect(page.locator("#downstream-error")).to_contain_text("issuance is unknown")
        expect(page.locator("#downstream-error")).to_contain_text(f"Client {label} · Read models")
        page.locator("[data-close='downstream-dialog']").click()
        page.unroute("**/admin/api/v1/downstream-tokens", commit_then_abort)
        page.locator("#recommended-action").click()
        expect(page.locator("#decision-title")).to_have_text(
            f"Review unrecoverable token for {label}"
        )
        expect(page.locator("#issue-downstream")).to_be_disabled()
        expect(page.locator("#issue-downstream-reason")).to_contain_text(
            "one-time response was lost"
        )
        _logout_and_reauthenticate_orphan_recovery(page, server.state)
        expect(page.locator("#decision-title")).to_have_text(
            f"Review unrecoverable token for {label}"
        )
        _reconcile_externally_revoked_orphan(page, server.state, label)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_lost_issue_already_revoked_requires_a_new_unique_label() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    label = "Lost then revoked client"
    route_finished = Event()
    commit_revoke_then_abort = partial(
        _commit_revoke_then_abort,
        state=server.state,
        label=label,
        finished=route_finished,
    )

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        page.locator(f"#key-{_FIRST_KEY_ID}-probe").click()
        page.locator(f"#key-{_FIRST_KEY_ID}-toggle").click()
        _ = page.route("**/admin/api/v1/downstream-tokens", commit_revoke_then_abort)
        page.locator("#issue-downstream").click()
        page.locator("#downstream-label").fill(label)
        page.locator("input[name='scope']").first.check()
        page.locator("#submit-downstream").click()
        expect(page.locator("#downstream-error")).to_contain_text("issuance is unknown")
        for _ in range(100):
            if route_finished.is_set():
                break
            page.wait_for_timeout(10)
        assert route_finished.is_set()
        page.locator("[data-close='downstream-dialog']").click()
        page.unroute("**/admin/api/v1/downstream-tokens", commit_revoke_then_abort)
        page.locator("#recommended-action").click()
        expect(page.locator("#last-result")).to_contain_text("is already revoked")
        expect(page.locator("#last-result")).to_contain_text("label remains reserved")
        expect(page.locator("#decision-title")).not_to_contain_text("Revoke token")
        expect(page.locator("#issue-downstream")).to_be_enabled()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def _assert_exact_lost_add_reconciliation(page: Page, handle: str) -> None:
    result = page.locator("#last-result")
    expect(result).to_contain_text(f"Fresh state confirms Key {handle} exists, is disabled")
    expect(result).to_contain_text("routing state disabled")
    expect(result).to_contain_text("fingerprint matches the submitted key")
    expect(result).not_to_contain_text("Multiple new")


def _reauthenticate_lost_add(page: Page, state: FakeAdminState, handle: str) -> None:
    page.locator("#navigation-disclosure summary").click()
    page.locator("#logout").click()
    expect(page.locator("#login-interrupted")).to_contain_text(
        f"Adding submitted Key {handle} was interrupted"
    )
    page.locator("#admin-bearer").fill(state.admin_bearer)
    page.keyboard.press("Enter")
    _assert_exact_lost_add_reconciliation(page, handle)


def test_lost_add_response_uses_submitted_fingerprint_amid_concurrent_add() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    submitted = "synthetic-lost-add-exact-correlation"
    concurrent = "synthetic-concurrent-unrelated-add"
    handle = sha256(submitted.encode()).hexdigest()[:8]

    def commit_add_race_then_abort(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        _ = route.fetch()
        _ = server.state.add_upstream(UpstreamKeyCreateRequest(key=concurrent))
        route.abort()

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        _ = page.route("**/admin/api/v1/upstream-keys", commit_add_race_then_abort)
        page.locator("#add-upstream").click()
        page.locator("#upstream-key").fill(submitted)
        page.locator("#submit-upstream").click()
        expect(page.locator("#upstream-error")).to_contain_text("was lost")
        expect(page.locator("#upstream-error")).to_contain_text(f"Key {handle}")
        expect(page.locator("#upstream-error")).to_contain_text("editable field is a new draft")
        expect(page.locator("#upstream-error")).to_contain_text(
            "success is identified only by that submitted fingerprint"
        )
        expect(page.locator("#upstream-error")).to_be_focused()
        expect(page.locator("#upstream-key")).not_to_have_attribute("aria-invalid", "true")
        expect(page.locator("#upstream-dialog-stale")).to_be_hidden()
        page.locator("#upstream-key").fill("safe-reentry-does-not-hide-recovery")
        expect(page.locator("#upstream-error")).to_contain_text("was lost")
        page.locator("[data-close='upstream-dialog']").click()
        page.unroute("**/admin/api/v1/upstream-keys", commit_add_race_then_abort)
        _reauthenticate_lost_add(page, server.state, handle)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def _assert_add_service_failure(page: Page) -> None:
    expect(page.locator("#upstream-dialog-stale")).to_be_hidden()
    expect(page.locator("#submit-upstream")).to_be_disabled()
    expect(page.locator("#last-result")).to_have_text("No action has completed in this tab.")
    expect(page.locator("#operation-status-line")).to_be_visible()
    expect(page.locator("#operation-status")).to_contain_text("result is unknown")


def _assert_issue_service_failure_and_refresh(page: Page) -> None:
    expect(page.locator("#downstream-error")).to_be_focused()
    expect(page.locator("#downstream-label")).not_to_have_attribute("aria-invalid", "true")
    expect(page.locator("#downstream-scopes")).not_to_have_attribute("aria-invalid", "true")
    page.locator("#downstream-label").press("End")
    page.locator("#downstream-label").press_sequentially(" updated")
    expect(page.locator("#downstream-error")).to_contain_text("Cancel and refresh")
    expect(page.locator("#downstream-dialog-stale")).to_be_hidden()
    expect(page.locator("#submit-downstream")).to_be_disabled()
    expect(page.locator("#operation-status-line")).to_be_visible()
    expect(page.locator("#operation-status")).to_contain_text("result is unknown")
    page.locator("[data-close='downstream-dialog']").click()
    page.locator("#recommended-action").click()
    expect(page.locator("#last-result")).to_contain_text("no new token")
    expect(page.locator("#last-result")).not_to_contain_text("in progress")


def _exercise_confirm_service_failure(page: Page) -> None:
    path = f"**/admin/api/v1/upstream-keys/{_FIRST_KEY_ID}/disable"

    def unavailable(route: Route) -> None:
        route.fulfill(
            status=503,
            content_type="application/json",
            body=(
                '{"error":{"code":"database_unavailable","message":'
                '"synthetic unavailable","request_id":"synthetic-request"}}'
            ),
        )

    _ = page.route(path, unavailable)
    page.locator(f"#key-{_FIRST_KEY_ID}-toggle").click()
    page.locator("#confirm-action").click()
    expect(page.locator("#confirm-error")).to_be_focused()
    expect(page.locator(f"#result-{_FIRST_KEY_ID}")).to_contain_text("may have completed")
    expect(page.locator("#operation-status-line")).to_be_visible()
    page.locator("[data-close='confirm-dialog']").click()
    page.unroute(path, unavailable)
    page.locator("#recommended-action").click()
    expect(page.locator(f"#result-{_FIRST_KEY_ID}")).to_contain_text("remains enabled")
    expect(page.locator("#last-result")).not_to_contain_text("in progress")


def test_service_errors_are_dialog_owned_and_persist_across_field_edits() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")

        server.state.fail_next("/admin/api/v1/upstream-keys")
        page.locator("#add-upstream").click()
        page.locator("#upstream-key").fill("synthetic-service-error-key")
        page.locator("#submit-upstream").click()
        expect(page.locator("#upstream-error")).to_be_focused()
        expect(page.locator("#upstream-key")).not_to_have_attribute("aria-invalid", "true")
        page.locator("#upstream-key").fill("safe-new-input")
        expect(page.locator("#upstream-error")).to_be_visible()
        expect(page.locator("#upstream-error")).to_contain_text("Cancel and refresh")
        _assert_add_service_failure(page)
        page.locator("[data-close='upstream-dialog']").click()
        page.locator("#recommended-action").click()
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        expect(page.locator("#last-result")).to_contain_text("does not contain the submitted key")

        _enable_first_key(page)
        server.state.fail_next("/admin/api/v1/downstream-tokens")
        page.locator("#issue-downstream").click()
        page.locator("#downstream-label").fill("Service error client")
        page.locator("input[name='scope']").first.check()
        page.locator("#submit-downstream").click()
        _assert_issue_service_failure_and_refresh(page)
        _exercise_confirm_service_failure(page)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


@pytest.mark.parametrize(("phase", "malformed"), [("login", True), ("refresh", False)])
def test_invalid_admin_payload_has_a_safe_recovery_surface(
    phase: str,
    malformed: bool,
) -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def invalid_dashboard(route: Route) -> None:
        if malformed:
            route.fulfill(status=200, content_type="application/json", body="{")
            return
        response = route.fetch()
        source = response.text()
        assert source.endswith("}")
        route.fulfill(response=response, body=source[:-1] + ',"unexpected":true}')

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        if phase == "login":
            _ = page.route("**/admin/api/v1/dashboard", invalid_dashboard)
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        if phase == "refresh":
            expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
            _ = page.route("**/admin/api/v1/dashboard", invalid_dashboard)
            page.locator("#refresh-dashboard").click()
        expect(page.locator("#global-error")).to_be_visible()
        expect(page.locator("#global-error-state")).to_have_text("Current state not confirmed")
        expect(page.locator("#global-error-message")).to_contain_text("Invalid response")
        expected_title = (
            "Refresh current state" if phase == "login" else "Refresh before making changes"
        )
        expect(page.locator("#decision-title")).to_have_text(expected_title)
        page.unroute("**/admin/api/v1/dashboard", invalid_dashboard)
        page.locator("#retry-dashboard").click()
        expect(page.locator("#global-error")).to_be_hidden()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_runtime_not_ready_overrides_eligible_rows_and_locks_mutations() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def runtime_not_ready(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        overview = payload.overview.model_copy(
            update={"ready": False, "status": OverviewStatus.DEGRADED}
        )
        body = payload.model_copy(
            update={
                "runtime_state": RuntimeState.UNAVAILABLE,
                "readiness_cause": ReadinessCause.RUNTIME_UNAVAILABLE,
                "overview": overview,
            }
        ).model_dump_json()
        route.fulfill(response=response, body=body)

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        _enable_first_key(page)
        _ = page.route("**/admin/api/v1/dashboard", runtime_not_ready)
        _refresh_dashboard_from(page, "#refresh-dashboard")
        expect(page.locator("#gateway-status")).to_have_text(
            "Not ready · Local runtime unavailable"
        )
        expect(page.locator("#decision-state")).to_contain_text("Outage")
        expect(page.locator("#decision-title")).to_have_text("Restore the local service")
        expect(page.locator("#recommended-action")).to_have_text("Refresh after service recovery")
        assert page.locator("[data-mutation]:enabled").count() == 0
        execute_script(
            page,
            """() => {
const originalFetch = window.fetch.bind(window);
globalThis.__recommendedRefreshCalls = 0;
window.fetch = (input, options) => {
  if (String(input).includes('/admin/api/v1/dashboard')) {
    globalThis.__recommendedRefreshCalls += 1;
    return new Promise((resolve) => setTimeout(() => resolve(originalFetch(input, options)), 250));
  }
  return originalFetch(input, options);
};
}""",
        )
        execute_script(
            page,
            """() => {
const action = document.getElementById('recommended-action');
action.click();
action.click();
}""",
        )
        expect(page.locator("#recommended-action")).to_have_text("Refreshing…")
        expect(page.locator("#recommended-action")).to_be_disabled()
        expect(page.locator("#recommended-action")).to_have_attribute("aria-busy", "true")
        expect(page.locator("#recommended-action")).to_have_text("Refresh after service recovery")
        expect(page.locator("#recommended-action")).to_have_attribute("aria-busy", "false")
        assert page.evaluate("globalThis.__recommendedRefreshCalls") == 1
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_activity_pairs_started_and_terminal_attempt_into_one_attention_item() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def dashboard(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        event_time = datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC)
        start_id = UUID("00000000-0000-4000-8000-000000000031")
        fingerprint = payload.upstream_keys.items[0].fingerprint
        events = AdminDashboardEventListResponse(
            items=(
                AdminDashboardEventRead(
                    id=UUID("00000000-0000-4000-8000-000000000032"),
                    request_id="paired-attempt",
                    event_type=EventType.UPSTREAM_ATTEMPT,
                    upstream_key_id=UUID(_FIRST_KEY_ID),
                    downstream_token_id=None,
                    outcome_class=EventOutcome.FAILED,
                    status_class=LastStatusClass.TIMEOUT,
                    latency_ms=25,
                    occurred_at=event_time,
                    upstream_key_fingerprint=fingerprint,
                    attempt_started_event_id=start_id,
                ),
                AdminDashboardEventRead(
                    id=start_id,
                    request_id="paired-attempt",
                    event_type=EventType.UPSTREAM_ATTEMPT,
                    upstream_key_id=UUID(_FIRST_KEY_ID),
                    downstream_token_id=None,
                    outcome_class=EventOutcome.STARTED,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                    upstream_key_fingerprint=fingerprint,
                    attempt_started_event_id=None,
                ),
            )
        )
        overview = payload.overview.model_copy(
            update={"last_event_at": event_time, "generated_at": event_time, "request_count": 1}
        )
        body = payload.model_copy(update={"events": events, "overview": overview}).model_dump_json()
        route.fulfill(response=response, body=body)

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        _ = page.route("**/admin/api/v1/dashboard", dashboard)
        page.locator("#refresh-dashboard").click()
        summary = page.locator("#activity-summary")
        expect(summary).to_contain_text("Key aaaaaaaa request failed")
        expect(summary).not_to_contain_text("no terminal result")
        assert summary.locator("li").count() == 1
        page.locator("#audit-events summary").click()
        assert page.locator("#events-body tr").count() == 2
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_activity_keeps_unpaired_failover_start_visible() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)

    def dashboard(route: Route) -> None:
        response = route.fetch()
        payload = AdminDashboardRead.model_validate_json(response.text())
        event_time = datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC)
        first_start_id = UUID("00000000-0000-4000-8000-000000000041")
        first_fingerprint = payload.upstream_keys.items[0].fingerprint
        second_fingerprint = payload.upstream_keys.items[1].fingerprint
        events = AdminDashboardEventListResponse(
            items=(
                AdminDashboardEventRead(
                    id=UUID("00000000-0000-4000-8000-000000000043"),
                    request_id="failover-request",
                    event_type=EventType.UPSTREAM_ATTEMPT,
                    upstream_key_id=UUID(_SECOND_KEY_ID),
                    downstream_token_id=None,
                    outcome_class=EventOutcome.STARTED,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=event_time,
                    upstream_key_fingerprint=second_fingerprint,
                    attempt_started_event_id=None,
                ),
                AdminDashboardEventRead(
                    id=UUID("00000000-0000-4000-8000-000000000042"),
                    request_id="failover-request",
                    event_type=EventType.UPSTREAM_ATTEMPT,
                    upstream_key_id=UUID(_FIRST_KEY_ID),
                    downstream_token_id=None,
                    outcome_class=EventOutcome.FAILED,
                    status_class=LastStatusClass.TIMEOUT,
                    latency_ms=25,
                    occurred_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
                    upstream_key_fingerprint=first_fingerprint,
                    attempt_started_event_id=first_start_id,
                ),
                AdminDashboardEventRead(
                    id=first_start_id,
                    request_id="failover-request",
                    event_type=EventType.UPSTREAM_ATTEMPT,
                    upstream_key_id=UUID(_FIRST_KEY_ID),
                    downstream_token_id=None,
                    outcome_class=EventOutcome.STARTED,
                    status_class=None,
                    latency_ms=None,
                    occurred_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                    upstream_key_fingerprint=first_fingerprint,
                    attempt_started_event_id=None,
                ),
            )
        )
        overview = payload.overview.model_copy(
            update={
                "last_event_at": event_time,
                "generated_at": event_time,
                "request_count": 1,
            }
        )
        route.fulfill(
            response=response,
            body=payload.model_copy(
                update={"events": events, "overview": overview}
            ).model_dump_json(),
        )

    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        _ = page.route("**/admin/api/v1/dashboard", dashboard)
        page.locator("#refresh-dashboard").click()
        summary = page.locator("#activity-summary")
        expect(summary).to_contain_text("Key aaaaaaaa request failed")
        expect(summary).to_contain_text(
            "Key bbbbbbbb request completion unconfirmed · Do not retry from this view"
        )
        assert summary.locator("li").count() == 2
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_effective_native_zoom_viewport_shows_judgment_action_and_safe_dialog() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 640, "height": 450})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        page.locator("#dashboard-title").wait_for(state="visible")
        geometry = _DecisionGeometry.model_validate_json(
            evaluate_string(
                page,
                """() => {
              const title = document.querySelector('#decision-title').getBoundingClientRect();
              const action = document.querySelector('#recommended-action').getBoundingClientRect();
              return JSON.stringify({height: visualViewport.height, title_bottom: title.bottom,
                      action_bottom: action.bottom,
                      primary: [...document.querySelectorAll('.control.primary')]
                        .filter((node) => node.getClientRects().length && !node.disabled).length});
            }""",
            )
        )
        assert geometry.title_bottom <= geometry.height
        assert geometry.action_bottom <= geometry.height
        assert geometry.primary == 1
        page.locator("#add-upstream").click()
        expect(page.locator("#upstream-key")).to_be_focused()
        dialog = _DialogGeometry.model_validate_json(
            evaluate_string(
                page,
                """() => { const rect = document.querySelector('#upstream-dialog')
                  .getBoundingClientRect();
                  return JSON.stringify({top: rect.top, bottom: rect.bottom,
                    height: visualViewport.height}); }""",
            )
        )
        assert dialog.top >= 0
        assert dialog.bottom <= dialog.height
        page.locator("[data-close='upstream-dialog']").click()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_snapshot_expires_at_known_cooldown_transition_and_moves_focus_to_refresh() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 375, "height": 812})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _open_admin_with_paused_clock(page, server.state.admin_bearer)
        page.locator(f"#key-{_FIRST_KEY_ID}-probe").click()
        expect(page.locator("#decision-title")).to_have_text("Enable Key aaaaaaaa")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        _install_decision_scenario(page, "short-cooldown", server.state)
        _refresh_dashboard_from(page, "#refresh-dashboard")
        expect(page.locator("#decision-title")).to_have_text("Wait for Key bbbbbbbb cooldown")
        page.locator("#add-upstream").focus()
        expect(page.locator("#add-upstream")).to_be_focused()
        page.clock.run_for(800)
        expect(page.locator("#decision-state")).to_contain_text("Stale")
        expect(page.locator("#recommended-action")).to_have_text("Refresh current state")
        expect(page.locator("#recommended-action")).to_be_focused()
        expect(page.locator(f"#key-{_SECOND_KEY_ID}-probe")).to_be_disabled()
        row = page.locator(f"#key-{_SECOND_KEY_ID}-probe").locator("xpath=ancestor::tr")
        expect(row).to_contain_text("Last confirmed")
        expect(page.locator(f"#key-{_SECOND_KEY_ID}-probe-reason")).to_contain_text(
            "Last confirmed cooldown deadline"
        )
        expect(row).not_to_contain_text("Returns automatically")
        expect(page.locator("#activity-summary")).not_to_contain_text("Just now")
        expect(page.locator("#activity-summary")).to_contain_text("Observed 2026-01-01")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


@pytest.mark.parametrize(
    ("scenario", "title", "action"),
    [
        ("normal", "No action required", None),
        ("quarantined", "Probe Key bbbbbbbb", "Probe Key bbbbbbbb"),
        ("invalid", "Retire rejected Key bbbbbbbb", "Review Key bbbbbbbb"),
        ("invalid-no-client", "Retire rejected Key bbbbbbbb", "Review Key bbbbbbbb"),
        (
            "credits",
            "Restore credits for Key bbbbbbbb",
            "Probe Key bbbbbbbb after restoring credits",
        ),
        ("cooldown", "Wait for Key bbbbbbbb cooldown", None),
        ("disabled-cooldown", "Wait before probing Key bbbbbbbb", None),
        ("ended-cooldown", "No action required", None),
        ("disabled-ended-cooldown", "Probe Key bbbbbbbb", "Probe Key bbbbbbbb"),
        ("disabled", "No action required", None),
    ],
)
def test_decision_matrix_exposes_cause_target_and_one_action(  # noqa: PLR0915
    scenario: str,
    title: str,
    action: str | None,
) -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        _install_decision_scenario(page, scenario, server.state)
        page.locator("#refresh-dashboard").click()
        expect(page.locator("#decision-title")).to_have_text(title)
        if action is None:
            expect(page.locator("#recommended-action")).to_be_hidden()
        else:
            expect(page.locator("#recommended-action")).to_have_text(action)
            expect(page.locator("#recommended-action")).to_be_visible()
        assert page.locator(".control.primary:visible").count() == (0 if action is None else 1)
        if scenario in {"cooldown", "disabled-cooldown"}:
            probe = page.locator(f"#key-{_SECOND_KEY_ID}-probe")
            expect(probe).to_be_disabled()
            expect(probe).to_have_attribute(
                "aria-describedby", f"key-{_SECOND_KEY_ID}-probe-reason"
            )
            expect(page.locator(f"#key-{_SECOND_KEY_ID}-probe-reason")).to_contain_text(
                "cooldown ends"
            )
            expect(page.locator(f"#key-{_SECOND_KEY_ID}-probe-reason")).to_contain_text("in 5m")
        if scenario == "invalid":
            expect(page.locator(f"#key-{_SECOND_KEY_ID}-probe")).to_be_disabled()
        if scenario == "invalid-no-client":
            expect(page.locator("#issue-downstream")).to_be_disabled()
            expect(page.locator("#issue-downstream-reason")).to_contain_text(
                "exactly two registered keys"
            )
        if scenario == "credits":
            probe = page.locator(f"#key-{_SECOND_KEY_ID}-probe")
            expect(probe).to_be_enabled()
            expect(probe).to_have_text("Probe after credits")
            expect(page.locator(f"#key-{_SECOND_KEY_ID}-probe-reason")).to_contain_text(
                "console cannot observe that external change"
            )
        if scenario == "ended-cooldown":
            row = page.locator(f"#key-{_SECOND_KEY_ID}-probe").locator("xpath=ancestor::tr")
            expect(row).to_contain_text("Cooldown ended · Eligible for new requests")
        if scenario == "disabled-ended-cooldown":
            row = page.locator(f"#key-{_SECOND_KEY_ID}-probe").locator("xpath=ancestor::tr")
            expect(row).to_contain_text("Cooldown ended · Probe the disabled key")
            expect(page.locator(f"#key-{_SECOND_KEY_ID}-probe")).to_be_enabled()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_first_token_gate_flows_from_one_to_two_eligible_keys() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    pattern = "**/admin/api/v1/dashboard"
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")

        _install_decision_scenario(page, "invalid-no-client", server.state)
        _refresh_dashboard_from(page, "#refresh-dashboard")
        expect(page.locator("#eligible-count")).to_have_text("2 registered · 1 eligible")
        expect(page.locator("#issue-downstream")).to_be_disabled()
        expect(page.locator("#issue-downstream-reason")).to_contain_text(
            "exactly two registered keys"
        )
        expect(page.locator("#recommended-action")).to_have_text("Review Key bbbbbbbb")

        page.unroute(pattern)
        _install_decision_scenario(page, "normal-no-client", server.state)
        _refresh_dashboard_from(page, "#refresh-dashboard")
        expect(page.locator("#eligible-count")).to_have_text("2 registered · 2 eligible")
        expect(page.locator("#issue-downstream")).to_be_enabled()
        expect(page.locator("#issue-downstream-reason")).to_be_hidden()
        expect(page.locator("#recommended-action")).to_have_text("Issue downstream token")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_invalid_key_with_existing_eligible_retires_after_reauthentication() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        _install_decision_scenario(page, "invalid", server.state)
        page.locator("#refresh-dashboard").click()
        expect(page.locator("#decision-title")).to_have_text("Retire rejected Key bbbbbbbb")
        page.locator("#navigation-disclosure summary").click()
        page.locator("#logout").click()
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#decision-title")).to_have_text("Retire rejected Key bbbbbbbb")
        _start_reviewed_row_action(page, f"key-{_SECOND_KEY_ID}-toggle")
        expect(page.locator("#confirm-title")).to_have_text("Disable upstream key")
        page.locator("#confirm-action").click()
        expect(page.locator("#decision-title")).to_have_text("Retire rejected Key bbbbbbbb")
        _start_reviewed_row_action(page, f"key-{_SECOND_KEY_ID}-delete")
        expect(page.locator("#confirm-title")).to_have_text("Delete upstream key")
        page.locator("#confirm-action").click()
        expect(page.locator("#decision-title")).to_have_text("Add the second upstream key")
        expect(page.locator("#recommended-action")).to_have_text("Add second key")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_tracked_replacement_retires_invalid_key_when_it_is_the_only_eligible_key() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    replacement = "synthetic-only-eligible-replacement"
    replacement_handle = sha256(replacement.encode()).hexdigest()[:8]
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        _install_decision_scenario(page, "invalid-only", server.state)
        page.locator("#refresh-dashboard").click()
        expect(page.locator("#decision-title")).to_have_text("Replace Key bbbbbbbb")
        page.locator("#recommended-action").click()
        expect(page.locator("#upstream-title")).to_have_text("Replace Key bbbbbbbb")
        expect(page.locator("#upstream-description")).to_contain_text("Key bbbbbbbb")
        expect(page.locator("#submit-upstream")).to_have_text("Add replacement")
        page.locator("#upstream-key").fill(replacement)
        page.locator("#submit-upstream").click()
        expect(page.locator("#last-result")).to_contain_text("replacement for Key bbbbbbbb")
        expect(page.locator("#decision-title")).to_have_text(f"Probe Key {replacement_handle}")
        page.locator("#recommended-action").click()
        expect(page.locator("#decision-title")).to_have_text(f"Enable Key {replacement_handle}")
        page.locator("#recommended-action").click()
        expect(page.locator("#eligible-count")).to_have_text("3 registered · 1 eligible")
        expect(page.locator("#decision-title")).to_have_text("Disable replaced Key bbbbbbbb")
        _start_reviewed_row_action(page, f"key-{_SECOND_KEY_ID}-toggle")
        page.locator("#confirm-action").click()
        expect(page.locator("#decision-title")).to_have_text("Delete replaced Key bbbbbbbb")
        _start_reviewed_row_action(page, f"key-{_SECOND_KEY_ID}-delete")
        page.locator("#confirm-action").click()
        expect(page.locator("#decision-title")).to_have_text("Probe Key aaaaaaaa")
        expect(page.locator("#decision-title")).not_to_contain_text("Add replacement")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_row_replace_tracks_source_through_three_row_cleanup() -> None:  # noqa: PLR0915
    server = start_fake_server()
    _ = server.state.change_upstream(UUID(_FIRST_KEY_ID), "probe")
    _ = server.state.change_upstream(UUID(_FIRST_KEY_ID), "enable")
    _ = server.state.change_upstream(UUID(_SECOND_KEY_ID), "probe")
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    replacement = "synthetic-healthy-row-replacement"
    replacement_handle = sha256(replacement.encode()).hexdigest()[:8]
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
        expect(page.locator("#eligible-count")).to_have_text("2 registered · 2 eligible")

        page.locator(f"#key-{_FIRST_KEY_ID}-replace").click()
        expect(page.locator("#upstream-title")).to_have_text("Replace Key aaaaaaaa")
        page.locator("#upstream-key").fill(replacement)
        page.locator("#submit-upstream").click()
        expect(page.locator("#decision-title")).to_have_text(f"Probe Key {replacement_handle}")
        page.locator("#recommended-action").click()
        expect(page.locator("#decision-title")).to_have_text(f"Enable Key {replacement_handle}")
        page.locator("#recommended-action").click()

        expect(page.locator("#eligible-count")).to_have_text("3 registered · 3 eligible")
        expect(page.locator("#add-upstream")).to_be_disabled()
        expect(page.locator("#add-upstream")).to_have_attribute(
            "aria-describedby", "add-upstream-reason"
        )
        expect(page.locator("#add-upstream-reason")).to_have_text(
            "Return to exactly two registered upstream keys before adding another key."
        )
        expect(page.locator("#issue-downstream")).to_be_disabled()
        expect(page.locator("#issue-downstream")).to_have_attribute(
            "aria-describedby", "issue-downstream-reason"
        )
        expect(page.locator("#issue-downstream-reason")).to_have_text(
            "Return to exactly two registered upstream keys before issuing a downstream token."
        )
        upstreams_before_disabled_clicks = server.state.upstreams().items
        tokens_before_disabled_clicks = server.state.tokens().items
        server.state.reset_network_audit()
        page.locator("#add-upstream").evaluate("button => button.click()")
        page.locator("#issue-downstream").evaluate("button => button.click()")
        assert server.state.upstreams().items == upstreams_before_disabled_clicks
        assert server.state.tokens().items == tokens_before_disabled_clicks
        assert not any(item.method == "POST" for item in server.state.network_audit())
        expect(page.locator("#upstream-dialog")).to_be_hidden()
        expect(page.locator("#downstream-dialog")).to_be_hidden()
        expect(page.locator("#decision-title")).to_have_text("Disable replaced Key aaaaaaaa")
        _start_reviewed_row_action(page, f"key-{_FIRST_KEY_ID}-toggle")
        page.locator("#confirm-action").click()
        expect(page.locator("#decision-title")).to_have_text("Delete replaced Key aaaaaaaa")
        _start_reviewed_row_action(page, f"key-{_FIRST_KEY_ID}-delete")
        page.locator("#confirm-action").click()

        expect(page.locator("#eligible-count")).to_have_text("2 registered · 2 eligible")
        expect(page.locator(f"#key-{_FIRST_KEY_ID}-probe")).to_have_count(0)
        expect(page.locator("#decision-title")).to_have_text("No action required")
        expect(page.locator("[id$='-replace']")).to_have_count(2)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_untracked_three_row_state_requires_review_without_guessing_target() -> None:
    server = start_fake_server()
    _ = server.state.add_upstream(UpstreamKeyCreateRequest(key="synthetic-untracked-extra-key"))
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")

        expect(page.locator("#decision-state")).to_have_text("Cleanup required · 3 registered keys")
        expect(page.locator("#decision-title")).to_have_text("Return to exactly two upstream keys")
        expect(page.locator("#recommended-action")).to_have_text("Review extra upstream keys")
        expect(page.locator("#add-upstream")).to_be_disabled()
        expect(page.locator("#add-upstream")).to_have_attribute(
            "aria-describedby", "add-upstream-reason"
        )
        expect(page.locator("#add-upstream-reason")).to_have_text(
            "Return to exactly two registered upstream keys before adding another key."
        )
        expect(page.locator("#issue-downstream")).to_be_disabled()
        expect(page.locator("#issue-downstream")).to_have_attribute(
            "aria-describedby", "issue-downstream-reason"
        )
        expect(page.locator("#issue-downstream-reason")).to_have_text(
            "Return to exactly two registered upstream keys before issuing a downstream token."
        )
        upstreams_before_disabled_clicks = server.state.upstreams().items
        tokens_before_disabled_clicks = server.state.tokens().items
        server.state.reset_network_audit()
        page.locator("#add-upstream").evaluate("button => button.click()")
        page.locator("#issue-downstream").evaluate("button => button.click()")
        assert server.state.upstreams().items == upstreams_before_disabled_clicks
        assert server.state.tokens().items == tokens_before_disabled_clicks
        assert not any(item.method == "POST" for item in server.state.network_audit())
        expect(page.locator("#upstream-dialog")).to_be_hidden()
        expect(page.locator("#downstream-dialog")).to_be_hidden()
        expect(page.locator("[id$='-replace']")).to_have_count(0)
        page.locator("#recommended-action").click()
        expect(page.locator("#upstream-heading")).to_be_focused()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)
