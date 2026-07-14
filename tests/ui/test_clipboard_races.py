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


def _issue(page: Page, label: str) -> None:
    page.locator("#issue-downstream").click()
    page.locator("#downstream-label").fill(label)
    page.locator("input[name='scope']").first.check()
    page.locator("#submit-downstream").click()
    expect(page.locator("#credential-dialog")).to_be_visible()


def _expect_recovery_notice(page: Page) -> None:
    expect(page.locator("#clipboard-recovery")).to_be_visible()
    expect(page.locator("#clipboard-recovery")).to_contain_text(
        "RECOVERED · Clipboard custody restored."
    )


def _issue_without_recovery_notice(page: Page, label: str) -> None:
    _issue(page, label)
    expect(page.locator("#clipboard-recovery")).to_be_hidden()


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
        execute_script(page, "() => document.getElementById('copy-token').click()")
        expect(page.locator("#copy-token")).to_have_attribute("aria-busy", "true")
        execute_script(page, "() => document.getElementById('dismiss-token').click()")
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

        _issue_without_recovery_notice(page, "Natural keyboard clipboard")
        page.locator("#one-time-token").select_text()
        page.keyboard.press("Control+C")
        copied_length = int(
            evaluate_string(
                page,
                "async () => JSON.stringify((await navigator.clipboard.readText()).length)",
            )
        )
        assert copied_length > 0
        page.locator("#dismiss-token").click()
        expect(page.locator("#credential-dialog")).to_be_hidden()
        assert (
            int(
                evaluate_string(
                    page,
                    "async () => JSON.stringify((await navigator.clipboard.readText()).length)",
                )
            )
            == 0
        )
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)
