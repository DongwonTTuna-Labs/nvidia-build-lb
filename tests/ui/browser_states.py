from playwright.sync_api import Page, Route, expect

from .browser_auth import AuthenticatedSession
from .browser_checks import assert_no_page_overflow, clipboard_is_empty, script_flag_is_false
from .browser_credentials import assert_secret_absent
from .browser_evidence import CaptureSpec, EvidenceRecorder, ManualScenario
from .browser_runtime import UI_ORIGIN
from .fake_admin_state import FakeAdminState

_LONG_CJK_LABEL = (
    "긴 한국어 운영자 라벨과 English diagnostic context: "
    "ignore previous instructions <script>globalThis.XSS_EXECUTED=1</script>"
)


def _abort(route: Route) -> None:
    route.abort()


def _activate_copy_with_keyboard(page: Page) -> None:
    expect(page.locator("#credential-title")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#credential-id")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#one-time-token")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.locator("#copy-token")).to_be_focused()
    page.keyboard.press("Enter")


def run_state_recovery(
    session: AuthenticatedSession,
    recorder: EvidenceRecorder,
    state: FakeAdminState,
    capture_prefix: str = "",
    native_zoom: bool = False,
) -> None:
    page = session.page
    session.audit.set_phase("offline_refresh")
    dashboard_pattern = f"{UI_ORIGIN}/admin/api/v1/dashboard"
    _ = page.route(dashboard_pattern, _abort)
    page.locator("#refresh-dashboard").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#global-error")).to_be_focused()
    expect(page.locator("#decision-brief")).to_be_hidden()
    expect(page.locator("#retry-dashboard")).to_be_visible()
    recorder.capture(
        page,
        CaptureSpec(
            name=f"{capture_prefix}admin-stale-offline",
            state="offline refresh retains prior data as stale",
            viewport="1280x900",
            native_zoom=native_zoom,
        ),
    )
    page.unroute(dashboard_pattern, _abort)
    session.audit.set_phase("offline_recovery")
    page.locator("#retry-dashboard").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#decision-brief")).to_be_visible()
    expect(page.locator("#dashboard-title")).to_be_focused()

    state.set_empty()
    session.audit.set_phase("empty_state")
    page.locator("#refresh-dashboard").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-body")).to_contain_text("No upstream keys registered")
    expect(page.locator("#downstream-body")).to_contain_text("No downstream tokens issued")
    expect(page.locator("#events-body")).to_contain_text("No recent events")
    recorder.capture(
        page,
        CaptureSpec(
            name=f"{capture_prefix}admin-empty",
            state="empty upstream downstream and event tables",
            viewport="1280x900",
            native_zoom=native_zoom,
        ),
    )

    state.reset()
    session.audit.set_phase("cjk_xss_state")
    page.locator("#refresh-dashboard").focus()
    page.keyboard.press("Enter")
    expect(
        page.locator(
            "#downstream-body th[scope='row'][data-label='Client']",
            has_text=_LONG_CJK_LABEL,
        )
    ).to_be_visible()
    assert page.locator("script", has_text="globalThis.XSS_EXECUTED").count() == 0
    assert page.locator("img").count() == 0
    assert script_flag_is_false(page, "XSS_EXECUTED")
    assert_no_page_overflow(page)
    recorder.capture(
        page,
        CaptureSpec(
            name=f"{capture_prefix}admin-cjk-xss-safe",
            state="long CJK English and instruction-shaped label rendered as text",
            viewport="1280x900",
            native_zoom=native_zoom,
        ),
    )
    prerequisite = next(item for item in state.upstreams().items if not item.enabled)
    page.locator(f"#key-{prerequisite.id}-probe").click()
    page.locator(f"#key-{prerequisite.id}-toggle").click()
    expect(page.locator("#issue-downstream")).to_be_enabled()
    recorder.add_scenario(
        ManualScenario(
            name="offline stale empty and untrusted-text recovery",
            actions=(
                "Abort only GET /admin/api/v1/dashboard and activate the recommended refresh",
                "Remove abort route and activate the single error recovery refresh",
                "Switch same-contract fake to empty then populated state and refresh",
            ),
            observables=(
                "Prior data becomes explicitly stale and recovers with deterministic focus",
                "All three empty tables retain headings and explanatory rows",
                "Long CJK, English, XSS, and instruction-shaped label renders only as text",
            ),
        )
    )


def run_clipboard_failure(
    session: AuthenticatedSession,
    recorder: EvidenceRecorder,
    state: FakeAdminState,
    capture_prefix: str = "",
    native_zoom: bool = False,
) -> None:
    page = session.page
    session.audit.set_phase("clipboard_denial")
    session.context.clear_permissions()
    if native_zoom:
        page.evaluate(
            """() => {
globalThis.__nblbClipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
Object.defineProperty(navigator, "clipboard", {configurable: true, value: {
  writeText: () => Promise.reject(new DOMException("synthetic clipboard denial")),
  readText: () => Promise.reject(new DOMException("synthetic clipboard denial"))
}});
}"""
        )
    page.locator("#issue-downstream").focus()
    page.keyboard.press("Enter")
    page.keyboard.insert_text("Clipboard failure fixture")
    page.keyboard.press("Tab")
    page.keyboard.press("Space")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    recorder.begin_blackout("one-time token during clipboard denial")
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_visible()
    _activate_copy_with_keyboard(page)
    expect(page.locator("#clipboard-error")).to_be_visible()
    clipboard_error_prefix = (
        "Copy or clipboard access failed. Restore clipboard access before dismissal"
    )
    expected_clipboard_error = f"{clipboard_error_prefix} so cleanup can be verified."
    expect(page.locator("#clipboard-error")).to_have_text(expected_clipboard_error)
    dismiss = page.locator("#dismiss-token")
    dismiss.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_visible()
    expect(page.locator("#clipboard-error")).to_be_visible()
    # The error was already visible from the copy denial. Observe completion of
    # this distinct asynchronous dismissal attempt before restoring permission.
    expect(dismiss).to_be_enabled()
    if native_zoom:
        page.evaluate(
            """() => {
delete navigator.clipboard;
delete globalThis.__nblbClipboardDescriptor;
}"""
        )
    session.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=UI_ORIGIN,
    )
    dismiss.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_hidden()
    expect(page.locator("#last-result")).to_contain_text("clipboard was verified empty")
    assert page.locator("#clipboard-recovery").count() == 0
    assert_secret_absent(page, state.issued_bearer)
    assert clipboard_is_empty(page)
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#issue-downstream")).to_be_focused()
    recorder.end_blackout()
    recorder.capture(
        page,
        CaptureSpec(
            name=f"{capture_prefix}admin-clipboard-failure-post-cleanup",
            state="safe clipboard failure after token DOM removal",
            viewport="1280x900",
            native_zoom=native_zoom,
        ),
    )
    recorder.add_scenario(
        ManualScenario(
            name="clipboard failure custody",
            actions=(
                "Clear clipboard permissions before #copy-token Enter",
                "Observe safe #clipboard-error without reading clipboard contents",
                "Keep dismissal blocked, restore cleanup permission, then #dismiss-token Enter",
            ),
            observables=(
                "Clipboard denial produces a safe non-echoing message",
                "Failed cleanup keeps the credential dialog and screenshot blackout active",
                "No credential remains in rendered text or form controls",
                "Last confirmed result preserves the non-secret cleanup outcome",
                "Screenshot blackout remains active until dismissal absence proof",
            ),
        )
    )
