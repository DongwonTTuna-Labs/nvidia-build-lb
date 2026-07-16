from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.sync_api import Browser, BrowserContext, Page, expect

from .browser_checks import (
    AdminDesktopObservation,
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
from .browser_runtime import UI_ORIGIN
from .fake_admin_state import FakeAdminState


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    context: BrowserContext
    page: Page
    audit: PageAudit
    axe_serious: int
    axe_critical: int
    axe_network_requests: int
    desktop_layout: AdminDesktopObservation


@dataclass(frozen=True, slots=True)
class AuthJourneyContext:
    browser: Browser
    recorder: EvidenceRecorder
    state: FakeAdminState
    axe_asset: Path


class _Closable(Protocol):
    def close(self) -> None: ...


def close_page_context(page: _Closable, context: _Closable) -> None:
    try:
        page.close()
    finally:
        context.close()


def _capture_tablet_dialog(
    journey: AuthJourneyContext,
    page: Page,
    counts: AxeCounts,
    name: str,
    state: str,
) -> AxeCounts:
    assert_no_page_overflow(page)
    dialog_counts = axe_counts(page, journey.axe_asset)
    journey.recorder.capture_viewport(
        page,
        CaptureSpec(name=name, state=state, viewport="768x900 viewport"),
    )
    return AxeCounts(
        serious=counts.serious + dialog_counts.serious,
        critical=counts.critical + dialog_counts.critical,
        network_requests=counts.network_requests + dialog_counts.network_requests,
    )


def _capture_authenticated_tablet_dialogs(
    journey: AuthJourneyContext,
    page: Page,
    counts: AxeCounts,
) -> AxeCounts:
    target = next(item for item in journey.state.upstreams().items if not item.enabled)
    page.locator("#add-upstream").click()
    expect(page.locator("#upstream-key")).to_be_focused()
    counts = _capture_tablet_dialog(
        journey,
        page,
        counts,
        "admin-upstream-form-768",
        "empty upstream credential form at 768px",
    )
    page.locator("[data-close='upstream-dialog']").click()
    page.locator(f"#key-{target.id}-probe").click()
    page.locator(f"#key-{target.id}-toggle").click()
    expect(page.locator("#issue-downstream")).to_be_enabled()
    page.locator("#issue-downstream").click()
    expect(page.locator("#downstream-label")).to_be_focused()
    counts = _capture_tablet_dialog(
        journey,
        page,
        counts,
        "admin-downstream-form-768",
        "empty downstream token form at 768px",
    )
    page.locator("[data-close='downstream-dialog']").click()
    page.locator(f"#key-{target.id}-toggle").click()
    expect(page.locator("#confirm-title")).to_be_focused()
    counts = _capture_tablet_dialog(
        journey,
        page,
        counts,
        "admin-destructive-confirmation-768",
        "identified disable confirmation at 768px",
    )
    page.locator("[data-close='confirm-dialog']").click()
    return counts


def _capture_authenticated_narrow_surface(journey: AuthJourneyContext, width: int) -> AxeCounts:
    context = journey.browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    audit = PageAudit()
    try:
        page.set_default_timeout(5_000)
        audit.set_phase(f"authenticated_dashboard_{width}")
        audit.attach(page)
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        journey.recorder.begin_blackout("admin bearer entered in password control")
        page.keyboard.insert_text(journey.state.admin_bearer)
        page.keyboard.press("Enter")
        page.locator("#dashboard-title").wait_for(state="visible")
        assert_secret_absent(page, journey.state.admin_bearer)
        journey.recorder.end_blackout()
        assert_no_page_overflow(page)
        counts = axe_counts(page, journey.axe_asset)
        journey.recorder.capture(
            page,
            CaptureSpec(
                name=f"admin-dashboard-post-login-cleanup-{width}",
                state="authenticated dashboard after password DOM removal",
                viewport=f"{width}x900",
            ),
        )
        if width == 768:
            assert (
                page.locator("#upstream-keys table").evaluate(
                    "node => getComputedStyle(node).display"
                )
                == "table"
            )
        if width == 375:
            page.locator("#add-upstream").click()
            page.locator("#upstream-dialog").wait_for(state="visible")
            dialog_counts = axe_counts(page, journey.axe_asset)
            counts = AxeCounts(
                serious=counts.serious + dialog_counts.serious,
                critical=counts.critical + dialog_counts.critical,
                network_requests=counts.network_requests + dialog_counts.network_requests,
            )
            journey.recorder.capture(
                page,
                CaptureSpec(
                    name="admin-upstream-form-375",
                    state="empty upstream credential form before secret entry",
                    viewport="375x900",
                ),
            )
            page.locator("[data-close='upstream-dialog']").click()
        if width == 768:
            counts = _capture_authenticated_tablet_dialogs(journey, page, counts)
        assert_public_observability_clean(audit)
        return counts
    finally:
        journey.state.reset()
        journey.recorder.end_blackout()
        try:
            audit.detach()
        finally:
            close_page_context(page, context)


def _exercise_auth_failures(
    journey: AuthJourneyContext,
    page: Page,
    audit: PageAudit,
) -> None:
    audit.set_phase("auth_wrong_token")
    journey.recorder.begin_blackout("downstream credential entered in admin login")
    page.keyboard.insert_text(journey.state.downstream_bearer)
    page.keyboard.press("Enter")
    page.locator("#login-error").wait_for(state="visible")
    assert focused_id(page) == "login-error"
    assert_secret_absent(page, journey.state.downstream_bearer)
    journey.recorder.end_blackout()
    journey.recorder.capture(
        page,
        CaptureSpec(
            name="admin-login-error",
            state="safe authentication error after password reset",
            viewport="1280x900",
        ),
    )
    page.keyboard.press("Tab")
    assert focused_id(page) == "admin-bearer"
    journey.recorder.begin_blackout("admin bearer entered in password control")
    journey.state.fail_next("/admin/api/v1/dashboard")
    audit.set_phase("auth_initial_503")
    page.keyboard.insert_text(journey.state.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#global-error").wait_for(state="visible")
    assert focused_id(page) == "global-error"
    assert "Database unavailable" in page.locator("#global-error-message").inner_text()
    assert page.locator("#login-form").count() == 0
    assert page.locator("#admin-bearer").count() == 0
    assert_secret_absent(page, journey.state.admin_bearer)
    journey.recorder.end_blackout()
    journey.recorder.capture(
        page,
        CaptureSpec(
            name="admin-initial-503",
            state="initial safe database unavailable error after password DOM removal",
            viewport="1280x900",
        ),
    )
    audit.set_phase("auth_initial_503_recovery")
    page.keyboard.press("Tab")
    assert focused_id(page) == "retry-dashboard"
    page.keyboard.press("Enter")
    page.locator("#global-error").wait_for(state="hidden")
    page.locator("#upstream-body tr").first.wait_for(state="visible")
    assert focused_id(page) == "dashboard-title"


def open_authenticated_session(journey: AuthJourneyContext) -> AuthenticatedSession:
    narrow_counts = tuple(
        _capture_authenticated_narrow_surface(journey, width) for width in (375, 768)
    )
    journey.state.reset_network_audit()
    context = journey.browser.new_context(viewport={"width": 1280, "height": 900})
    context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=UI_ORIGIN,
    )
    page = context.new_page()
    page.set_default_timeout(5_000)
    audit = PageAudit()
    audit.attach(page)
    audit.set_phase("auth_shell")
    _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")

    _exercise_auth_failures(journey, page, audit)
    storage = storage_observation(page)
    assert storage.local == 0
    assert storage.session == 0
    assert storage.cookies == 0
    assert storage.query == 0
    assert_no_page_overflow(page)
    desktop_counts = axe_counts(page, journey.axe_asset)
    journey.recorder.capture(
        page,
        CaptureSpec(
            name="admin-dashboard-post-login-cleanup",
            state="authenticated dashboard after password DOM removal",
            viewport="1280x900",
        ),
    )
    counts = AxeCounts(
        serious=desktop_counts.serious + sum(item.serious for item in narrow_counts),
        critical=desktop_counts.critical + sum(item.critical for item in narrow_counts),
        network_requests=desktop_counts.network_requests
        + sum(item.network_requests for item in narrow_counts),
    )
    desktop_layout = admin_desktop_observation(page)
    journey.recorder.add_scenario(
        ManualScenario(
            name="keyboard-only login failure and success",
            actions=(
                "#admin-bearer keyboard text then Enter with downstream credential",
                "Tab from #login-error to restored #admin-bearer",
                "#admin-bearer Enter with admin credential while dashboard returns safe 503",
                "Tab from #global-error to #retry-dashboard, then Enter",
            ),
            observables=(
                "401 clears the password control and focuses #login-error",
                "503 removes login DOM, focuses #global-error, and exposes no credential",
                "Natural Tab reaches Retry; success focuses #dashboard-title",
                "URL, cookies, DOM, localStorage, and sessionStorage remain credential-free",
            ),
        )
    )
    return AuthenticatedSession(
        context=context,
        page=page,
        audit=audit,
        axe_serious=counts.serious,
        axe_critical=counts.critical,
        axe_network_requests=counts.network_requests,
        desktop_layout=desktop_layout,
    )


def close_authenticated_session(session: AuthenticatedSession) -> None:
    try:
        session.audit.detach()
    finally:
        close_page_context(session.page, session.context)
