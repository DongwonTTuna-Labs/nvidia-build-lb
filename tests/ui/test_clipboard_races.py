import pytest
from playwright.sync_api import Page, expect

from .browser_checks import evaluate_string, execute_script
from .browser_credentials import credential_state_observation
from .browser_runtime import (
    UI_ORIGIN,
    start_fake_server,
    start_managed_browser,
    stop_fake_server,
    stop_managed_browser,
)

pytestmark = pytest.mark.ui_fake
_UPSTREAM_ID = "00000000-0000-4000-8000-000000000001"


def _assert_credential_focus_contained(page: Page) -> None:
    contained = evaluate_string(
        page,
        """() => JSON.stringify(
document.getElementById("credential-dialog").contains(document.activeElement)
)""",
    )
    assert contained == "true"


def _issue(page: Page, label: str) -> None:
    page.locator("#issue-downstream").click()
    page.locator("#downstream-label").fill(label)
    page.locator("input[name='scope']").first.check()
    page.locator("#submit-downstream").click()
    expect(page.locator("#credential-dialog")).to_be_visible()


def _beforeunload_is_blocked(page: Page) -> bool:
    return (
        evaluate_string(
            page,
            """async () => {
const event = new Event('beforeunload', {cancelable:true});
const allowed = window.dispatchEvent(event);
return JSON.stringify(!allowed || event.defaultPrevented);
}""",
        )
        == "true"
    )


def _expect_recovery_notice(page: Page) -> None:
    expect(page.locator("#last-result")).to_contain_text("clipboard was verified empty")
    assert page.locator("#clipboard-recovery").count() == 0


def _issue_without_recovery_notice(page: Page, label: str) -> None:
    _issue(page, label)
    assert page.locator("#clipboard-recovery").count() == 0


def _exercise_absence_at_issue(page: Page) -> None:
    execute_script(
        page,
        """() => Object.defineProperty(
navigator, "clipboard", {configurable:true, value:undefined})""",
    )
    _issue_without_recovery_notice(page, "No clipboard API at issue")
    page.locator("#dismiss-token").click()
    expect(page.locator("#credential-dialog")).to_be_hidden()
    assert page.locator("#clipboard-recovery").count() == 0
    assert credential_state_observation(page).one_time_token_present is False
    execute_script(page, "() => delete navigator.clipboard")


def _exercise_absence_after_issue(page: Page) -> None:
    _issue(page, "Absent clipboard API")
    execute_script(
        page,
        """() => Object.defineProperty(
navigator, "clipboard", {configurable:true, value:undefined})""",
    )
    page.locator("#copy-token").click()
    expect(page.locator("#clipboard-error")).to_be_visible()
    assert credential_state_observation(page).copied_credential is False
    execute_script(page, "() => delete navigator.clipboard")
    page.locator("#dismiss-token").click()
    expect(page.locator("#credential-dialog")).to_be_hidden()
    _expect_recovery_notice(page)


def _exercise_visible_copy_completion(page: Page) -> None:
    _issue_without_recovery_notice(page, "Visible copy completion")
    assert credential_state_observation(page).one_time_token_present is True
    assert _beforeunload_is_blocked(page)
    page.locator("#copy-token").click()
    expect(page.locator("#copy-token")).to_have_text("Copy again")
    expect(page.locator("#copy-token")).to_be_focused()
    expect(page.locator("#credential-status")).to_contain_text("Dismiss removes it from the page")
    page.locator("#dismiss-token").click()
    expect(page.locator("#credential-dialog")).to_be_hidden()
    assert not _beforeunload_is_blocked(page)


def _exercise_keyboard_copy(page: Page) -> None:
    _issue_without_recovery_notice(page, "Natural keyboard clipboard")
    expect(page.locator("#credential-title")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#dismiss-token")).to_be_focused()
    page.locator("#one-time-token").focus()
    expect(page.locator("#one-time-token")).to_be_focused()
    page.keyboard.press("Control+A")
    assert (
        int(
            evaluate_string(
                page,
                """() => JSON.stringify(
document.getElementById("one-time-token").selectionEnd
  - document.getElementById("one-time-token").selectionStart
)""",
            )
        )
        > 0
    )
    page.keyboard.press("Control+C")
    expect(page.locator("#credential-status")).to_contain_text("copied manually")
    assert credential_state_observation(page).copied_credential is True
    assert _beforeunload_is_blocked(page)
    copied_length = int(
        evaluate_string(
            page,
            "async () => JSON.stringify((await navigator.clipboard.readText()).length)",
        )
    )
    assert copied_length > 0
    page.locator("#dismiss-token").click()
    expect(page.locator("#credential-dialog")).to_be_hidden()
    assert not _beforeunload_is_blocked(page)
    cleared_length = int(
        evaluate_string(
            page,
            "async () => JSON.stringify((await navigator.clipboard.readText()).length)",
        )
    )
    assert cleared_length == 0


def _exercise_rejected_held_write_dismiss(page: Page) -> None:
    _issue_without_recovery_notice(page, "Rejected clipboard write")
    execute_script(
        page,
        """() => {
let stored = "";
globalThis.__rejectHeldClipboard = null;
globalThis.__unhandledClipboardRejections = 0;
window.addEventListener("unhandledrejection", () => {
  globalThis.__unhandledClipboardRejections += 1;
});
Object.defineProperty(navigator, "clipboard", {configurable:true, value:{
  writeText: (value) => value === "" ? (stored = "", Promise.resolve()) :
    new Promise((_resolve, reject) => {
      globalThis.__rejectHeldClipboard = () => reject(
        new DOMException("synthetic clipboard rejection", "NotAllowedError")
      );
    }),
  readText: () => Promise.resolve(stored)
}});
}""",
    )
    page.locator("#copy-token").click()
    expect(page.locator("#copy-token")).to_have_attribute("aria-busy", "true")
    _ = page.wait_for_function("globalThis.__rejectHeldClipboard !== null")
    page.locator("#dismiss-token").click()
    expect(page.locator("#dismiss-token")).to_have_attribute("aria-busy", "true")
    execute_script(page, "() => globalThis.__rejectHeldClipboard()")

    expect(page.locator("#credential-dialog")).to_be_visible()
    assert credential_state_observation(page).one_time_token_present is True
    expect(page.locator("#clipboard-error")).to_be_visible()
    expect(page.locator("#clipboard-error")).to_be_focused()
    assert page.locator("#credential-dialog [role='alert']:visible").count() == 1
    expect(page.locator("#credential-busy")).to_be_hidden()
    expect(page.locator("#credential-dialog")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#copy-token")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#dismiss-token")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#copy-token")).to_be_enabled()
    expect(page.locator("#dismiss-token")).to_be_enabled()
    page.wait_for_timeout(50)
    assert (
        evaluate_string(
            page,
            "() => JSON.stringify(globalThis.__unhandledClipboardRejections)",
        )
        == "0"
    )

    execute_script(
        page,
        """() => {
let stored = "synthetic-previous-value";
Object.defineProperty(navigator, "clipboard", {configurable:true, value:{
  writeText: async (value) => { stored = value; },
  readText: async () => stored
}});
}""",
    )
    page.locator("#dismiss-token").click()
    expect(page.locator("#credential-dialog")).to_be_hidden()
    assert credential_state_observation(page).one_time_token_present is False
    assert (
        evaluate_string(
            page,
            "async () => JSON.stringify((await navigator.clipboard.readText()).length)",
        )
        == "0"
    )
    execute_script(
        page,
        """() => {
delete navigator.clipboard;
delete globalThis.__rejectHeldClipboard;
delete globalThis.__unhandledClipboardRejections;
}""",
    )


def test_clipboard_absence_held_write_dismiss_and_keyboard_copy_are_safe() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        page.keyboard.press("Enter")
        page.locator("#dashboard-title").wait_for(state="visible")
        page.locator(f"#key-{_UPSTREAM_ID}-probe").click()
        page.locator(f"#key-{_UPSTREAM_ID}-toggle").click()
        expect(page.locator("#issue-downstream")).to_be_enabled()

        _exercise_absence_at_issue(page)

        _exercise_absence_after_issue(page)

        _exercise_visible_copy_completion(page)

        _issue_without_recovery_notice(page, "Held clipboard write")
        execute_script(
            page,
            """() => {
let stored = "";
globalThis.__resolveHeldClipboard = null;
Object.defineProperty(navigator, "clipboard", {configurable:true, value:{
  writeText: (value) => value === "" ? (stored = "", Promise.resolve()) :
    new Promise((resolve) => {
      globalThis.__resolveHeldClipboard = () => { stored = value; resolve(); };
    }),
  readText: () => Promise.resolve(stored)
}});
}""",
        )
        page.locator("#copy-token").click()
        expect(page.locator("#copy-token")).to_have_attribute("aria-busy", "true")
        expect(page.locator("#credential-busy")).to_be_focused()
        page.keyboard.press("Tab")
        _assert_credential_focus_contained(page)
        page.keyboard.press("Shift+Tab")
        _assert_credential_focus_contained(page)
        page.locator("#dismiss-token").click()
        expect(page.locator("#credential-busy")).to_be_focused()
        expect(page.locator("#credential-dialog")).to_be_visible()
        execute_script(page, "() => globalThis.__resolveHeldClipboard()")
        expect(page.locator("#credential-dialog")).to_be_hidden()
        assert credential_state_observation(page).one_time_token_present is False
        assert (
            int(
                evaluate_string(
                    page,
                    "async () => JSON.stringify((await navigator.clipboard.readText()).length)",
                )
            )
            == 0
        )
        execute_script(
            page, "() => { delete navigator.clipboard; delete globalThis.__resolveHeldClipboard; }"
        )

        _exercise_rejected_held_write_dismiss(page)

        _exercise_keyboard_copy(page)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)
