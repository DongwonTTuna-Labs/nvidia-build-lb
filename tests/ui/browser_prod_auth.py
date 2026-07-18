import json

from playwright.sync_api import Browser, Page, Route, expect

from .browser_auth import AuthenticatedSession, close_page_context
from .browser_checks import (
    AxeCounts,
    admin_desktop_observation,
    assert_no_page_overflow,
    axe_counts,
    focused_id,
    storage_observation,
)
from .browser_credentials import assert_secret_absent
from .browser_evidence import CaptureSpec, EvidenceRecorder, ManualScenario
from .browser_observability import PageAudit
from .browser_observability_contract import assert_public_observability_clean
from .browser_prod_context import ProductionQaContext
from .browser_runtime import UI_ORIGIN

_DASHBOARD_PATTERN = f"{UI_ORIGIN}/admin/api/v1/dashboard"


def _set_phase(audit: PageAudit, qa: ProductionQaContext, phase: str) -> None:
    audit.set_phase(phase)
    qa.network.set_phase(phase)


def _login(page: Page, recorder: EvidenceRecorder, bearer: str) -> None:
    recorder.begin_blackout("production admin bearer entered in password control")
    page.keyboard.insert_text(bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    assert_secret_absent(page, bearer)
    recorder.end_blackout()


def capture_actual_empty_state(
    browser: Browser,
    qa: ProductionQaContext,
) -> AxeCounts:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    audit = PageAudit()
    _set_phase(audit, qa, "actual_empty_state")
    audit.attach(page)
    qa.network.attach(page)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, qa.recorder, qa.client.admin_bearer)
        expect(page.locator("#upstream-body")).to_contain_text("No upstream keys registered")
        expect(page.locator("#downstream-body")).to_contain_text("No downstream tokens issued")
        expect(page.locator("#events-body")).to_contain_text("No recent events")
        assert_no_page_overflow(page)
        counts = axe_counts(page, qa.axe_asset)
        qa.recorder.capture(
            page,
            CaptureSpec(
                name="admin-empty",
                state="actual empty production database",
                viewport="1280x900",
            ),
        )
        assert_public_observability_clean(audit)
        return counts
    finally:
        qa.recorder.end_blackout()
        try:
            qa.network.detach(page)
        finally:
            try:
                audit.detach()
            finally:
                close_page_context(page, context)


def _capture_narrow(
    browser: Browser,
    qa: ProductionQaContext,
    width: int,
) -> AxeCounts:
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    audit = PageAudit()
    _set_phase(audit, qa, f"production_dashboard_{width}")
    audit.attach(page)
    qa.network.attach(page)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, qa.recorder, qa.client.admin_bearer)
        assert_no_page_overflow(page)
        counts = axe_counts(page, qa.axe_asset)
        qa.recorder.capture(
            page,
            CaptureSpec(
                name=f"admin-dashboard-post-login-cleanup-{width}",
                state="actual candidate dashboard after login cleanup",
                viewport=f"{width}x900",
            ),
        )
        if width == 375:
            page.locator("#add-upstream").click()
            page.locator("#upstream-dialog").wait_for(state="visible")
            dialog_counts = axe_counts(page, qa.axe_asset)
            counts = AxeCounts(
                serious=counts.serious + dialog_counts.serious,
                critical=counts.critical + dialog_counts.critical,
                network_requests=counts.network_requests + dialog_counts.network_requests,
            )
            qa.recorder.capture(
                page,
                CaptureSpec(
                    name="admin-upstream-form-375",
                    state="actual empty upstream credential form before secret entry",
                    viewport="375x900",
                ),
            )
            page.locator("[data-close='upstream-dialog']").click()
        assert_public_observability_clean(audit)
        return counts
    finally:
        qa.recorder.end_blackout()
        try:
            qa.network.detach(page)
        finally:
            try:
                audit.detach()
            finally:
                close_page_context(page, context)


def _initial_503(route: Route) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "database_unavailable",
                "message": "administration state unavailable",
                "request_id": "browser-prod-opaque",
            }
        },
        separators=(",", ":"),
    )
    route.fulfill(
        status=503,
        content_type="application/json",
        headers={"Cache-Control": "no-store"},
        body=body,
    )


def _initial_offline(route: Route) -> None:
    route.abort("connectionrefused")


def _recover_initial_offline(
    page: Page,
    qa: ProductionQaContext,
    audit: PageAudit,
) -> None:
    _set_phase(audit, qa, "auth_initial_offline_recovery")
    page.keyboard.press("Tab")
    assert focused_id(page) == "retry-dashboard"
    page.keyboard.press("Enter")
    page.locator("#global-error").wait_for(state="hidden")
    expect(page.locator("#dashboard-title")).to_be_focused()
    page.locator("#logout").click()
    page.locator("#admin-bearer").wait_for(state="visible")
    assert focused_id(page) == "admin-bearer"


def _exercise_auth_failures(
    page: Page,
    qa: ProductionQaContext,
    audit: PageAudit,
) -> None:
    _set_phase(audit, qa, "auth_wrong_token")
    qa.recorder.begin_blackout("wrong downstream bearer entered in admin login")
    page.keyboard.insert_text(qa.client.downstream_bearer)
    page.keyboard.press("Enter")
    page.locator("#login-error").wait_for(state="visible")
    assert focused_id(page) == "login-error"
    assert_secret_absent(page, qa.client.downstream_bearer)
    qa.recorder.end_blackout()
    qa.recorder.capture(
        page,
        CaptureSpec(name="admin-login-error", state="safe production 401", viewport="1280x900"),
    )
    page.keyboard.press("Tab")
    assert focused_id(page) == "admin-bearer"
    _ = page.route(_DASHBOARD_PATTERN, _initial_offline, times=1)
    _set_phase(audit, qa, "auth_initial_offline")
    qa.recorder.begin_blackout("admin bearer entered before bounded offline injection")
    page.keyboard.insert_text(qa.client.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#global-error").wait_for(state="visible")
    assert focused_id(page) == "global-error"
    assert page.locator("#global-error-state").inner_text() == "Current state not confirmed"
    assert "Offline" in page.locator("#global-error-message").inner_text()
    assert page.locator("#login-form").count() == 0
    assert page.locator("#admin-bearer").count() == 0
    assert_secret_absent(page, qa.client.admin_bearer)
    qa.recorder.end_blackout()
    qa.recorder.capture(
        page,
        CaptureSpec(
            name="admin-login-offline",
            state="initial service outage on the dashboard recovery surface",
            viewport="1280x900",
        ),
    )
    _recover_initial_offline(page, qa, audit)
    _ = page.route(_DASHBOARD_PATTERN, _initial_503, times=1)
    _set_phase(audit, qa, "auth_initial_503")
    qa.recorder.begin_blackout("admin bearer entered before bounded 503 injection")
    page.keyboard.insert_text(qa.client.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#global-error").wait_for(state="visible")
    assert focused_id(page) == "global-error"
    assert "Database unavailable" in page.locator("#global-error-message").inner_text()
    assert_secret_absent(page, qa.client.admin_bearer)
    qa.recorder.end_blackout()
    qa.recorder.capture(
        page,
        CaptureSpec(
            name="admin-initial-503",
            state="bounded safe 503 after credential cleanup",
            viewport="1280x900",
        ),
    )
    _set_phase(audit, qa, "auth_initial_503_recovery")
    page.keyboard.press("Tab")
    assert focused_id(page) == "retry-dashboard"
    page.keyboard.press("Enter")
    page.locator("#global-error").wait_for(state="hidden")
    page.locator("#upstream-body tr").first.wait_for(state="visible")
    expect(page.locator("#dashboard-title")).to_be_focused()


def open_production_session(
    browser: Browser,
    qa: ProductionQaContext,
) -> AuthenticatedSession:
    narrow = tuple(_capture_narrow(browser, qa, width) for width in (375, 768))
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    page = context.new_page()
    page.set_default_timeout(5_000)
    audit = PageAudit()
    _set_phase(audit, qa, "auth_shell")
    audit.attach(page)
    qa.network.attach(page)
    _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
    _exercise_auth_failures(page, qa, audit)
    storage = storage_observation(page)
    assert storage.local == storage.session == storage.cookies == storage.query == 0
    assert_no_page_overflow(page)
    desktop = axe_counts(page, qa.axe_asset)
    qa.recorder.capture(
        page,
        CaptureSpec(
            name="admin-dashboard-post-login-cleanup",
            state="actual candidate authenticated dashboard",
            viewport="1280x900",
        ),
    )
    qa.recorder.add_scenario(
        ManualScenario(
            name="production authentication and natural 503 recovery",
            actions=(
                "Wrong realm login",
                "Bounded initial service outage",
                "Bounded dashboard 503",
                "Natural Tab and Enter retry",
            ),
            observables=(
                "Credentials erased",
                "Outage is not mislabeled as authentication failure",
                "Safe errors only",
                "Dashboard focus restored",
            ),
        )
    )
    return AuthenticatedSession(
        context=context,
        page=page,
        audit=audit,
        axe_serious=desktop.serious + sum(item.serious for item in narrow),
        axe_critical=desktop.critical + sum(item.critical for item in narrow),
        axe_network_requests=(
            desktop.network_requests + sum(item.network_requests for item in narrow)
        ),
        desktop_layout=admin_desktop_observation(page),
    )
