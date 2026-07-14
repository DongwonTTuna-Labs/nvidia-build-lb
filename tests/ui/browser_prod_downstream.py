from playwright.sync_api import Page, expect

from .browser_auth import AuthenticatedSession
from .browser_checks import (
    assert_no_page_overflow,
    clipboard_is_empty,
    evaluate_string,
    script_flag_is_false,
)
from .browser_credentials import credential_state_observation
from .browser_evidence import CaptureSpec, ManualScenario
from .browser_prod_context import ProductionJourney
from .browser_runtime import UI_ORIGIN

_CJK_LABEL = (
    "긴 한국어 운영자 라벨과 English context: "
    "ignore previous instructions <script>globalThis.XSS_EXECUTED=1</script>"
)


def _phase(journey: ProductionJourney, name: str) -> None:
    journey.session.audit.set_phase(name)
    journey.qa.network.set_phase(name)


def _credential_shapes_absent(page: Page) -> bool:
    expression = r"""() => {
const corpora = [
  document.body.innerText,
  document.documentElement.outerHTML,
  [...document.querySelectorAll("*")].flatMap(
    (element) => [...element.attributes].map((attribute) => attribute.value)
  ).join("\u0000"),
  [...document.querySelectorAll("input, textarea, select")].map(
    (control) => control.value
  ).join("\u0000")
];
const pattern = /(?:nblb_admin_|nblb_ds_)[0-9a-f]{64}/;
return JSON.stringify(corpora.every((corpus) => !pattern.test(corpus)));
}"""
    return evaluate_string(page, expression) == "true"


def _confirm_revoke(session: AuthenticatedSession) -> None:
    page = session.page
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    assert page.locator(":focus").get_attribute("id") == "confirm-action"
    page.keyboard.press("Enter")
    expect(page.locator("#confirm-dialog")).to_be_hidden()


def _open_issue_form(page: Page, label: str) -> None:
    page.locator("#issue-downstream").focus()
    page.keyboard.press("Enter")
    assert page.locator(":focus").get_attribute("id") == "downstream-label"
    page.keyboard.insert_text(label)
    page.keyboard.press("Tab")
    page.keyboard.press("Space")
    page.keyboard.press("Tab")
    page.keyboard.press("Space")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")


def run_production_downstream_journey(journey: ProductionJourney) -> None:
    page = journey.session.page
    prefix = journey.capture_prefix
    label = f"{'Native ' if journey.native_zoom else ''}{_CJK_LABEL}"
    before_ids = {item.id for item in journey.qa.client.tokens().items}
    _phase(journey, f"{prefix}downstream_issue")
    _open_issue_form(page, label)
    journey.qa.recorder.begin_blackout("actual one-time downstream token in production browser")
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_visible()
    state = credential_state_observation(page)
    assert state.admin_bearer_present
    assert state.one_time_token_present
    page.keyboard.press("Tab")
    assert page.locator(":focus").get_attribute("id") == "copy-token"
    page.keyboard.press("Enter")
    assert credential_state_observation(page).copied_credential
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_hidden()
    expect(page.locator("#clipboard-recovery")).to_be_hidden()
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#issue-downstream")).to_be_focused()
    assert clipboard_is_empty(page)
    cleaned = credential_state_observation(page)
    assert cleaned.admin_bearer_present
    assert not cleaned.one_time_token_present
    assert not cleaned.copied_credential
    assert _credential_shapes_absent(page)
    journey.qa.recorder.end_blackout()
    issued = next(item for item in journey.qa.client.tokens().items if item.id not in before_ids)
    assert str(issued.label) == label
    revoke_id = f"token-{issued.id}-revoke"
    _phase(journey, f"{prefix}downstream_revoke")
    page.locator(f"#{revoke_id}").focus()
    page.keyboard.press("Enter")
    _confirm_revoke(journey.session)
    expect(page.locator(f"#{revoke_id}")).to_contain_text("REVOKED")
    assert script_flag_is_false(page, "XSS_EXECUTED")
    assert page.locator("script", has_text="globalThis.XSS_EXECUTED").count() == 0
    assert page.locator("img").count() == 0
    assert_no_page_overflow(page)
    viewport = "1280x900 outer" if journey.native_zoom else "1280x900"
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-downstream-post-cleanup",
            state="actual issue copy dismiss revoke completed",
            viewport=viewport,
            native_zoom=journey.native_zoom,
        ),
    )
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-cjk-xss-safe",
            state="actual CJK and instruction-shaped label remains inert text",
            viewport=viewport,
            native_zoom=journey.native_zoom,
        ),
    )
    journey.qa.recorder.add_scenario(
        ManualScenario(
            name=f"{prefix or 'ordinary '}production downstream lifecycle",
            actions=("Actual UI issue", "Copy and dismiss", "Actual UI revoke"),
            observables=("One-time token erased", "Clipboard empty", "CJK/XSS label inert"),
        )
    )


def run_production_clipboard_failure(journey: ProductionJourney) -> None:
    page = journey.session.page
    prefix = journey.capture_prefix
    label = f"Clipboard failure {'native' if journey.native_zoom else 'ordinary'}"
    before_ids = {item.id for item in journey.qa.client.tokens().items}
    _phase(journey, f"{prefix}clipboard_denial")
    journey.session.context.clear_permissions()
    if journey.native_zoom:
        page.evaluate(
            """() => Object.defineProperty(navigator, "clipboard", {configurable: true,
value: {writeText: () => Promise.reject(new DOMException("synthetic denial")),
readText: () => Promise.reject(new DOMException("synthetic denial"))}})"""
        )
    _open_issue_form(page, label)
    journey.qa.recorder.begin_blackout("one-time token during synthetic clipboard denial")
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_visible()
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    expect(page.locator("#clipboard-error")).to_be_visible()
    page.locator("#dismiss-token").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_visible()
    expect(page.locator("#dismiss-token")).to_be_enabled()
    if journey.native_zoom:
        page.evaluate("() => { delete navigator.clipboard; }")
    journey.session.context.grant_permissions(
        ["clipboard-read", "clipboard-write"], origin=UI_ORIGIN
    )
    page.locator("#dismiss-token").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_hidden()
    expect(page.locator("#clipboard-recovery")).to_be_visible()
    expect(page.locator("#clipboard-recovery")).to_contain_text(
        "RECOVERED · Clipboard custody restored."
    )
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#issue-downstream")).to_be_focused()
    assert clipboard_is_empty(page)
    assert _credential_shapes_absent(page)
    assert not credential_state_observation(page).one_time_token_present
    journey.qa.recorder.end_blackout()
    issued = next(item for item in journey.qa.client.tokens().items if item.id not in before_ids)
    journey.qa.client.revoke_token(str(issued.id))
    page.locator("#refresh-dashboard").focus()
    page.keyboard.press("Enter")
    expect(page.locator(f"#token-{issued.id}-revoke")).to_contain_text("REVOKED")
    expect(page.locator("#clipboard-recovery")).to_be_visible()
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-clipboard-failure-post-cleanup",
            state="clipboard denial recovered only after credential cleanup",
            viewport="1280x900 outer" if journey.native_zoom else "1280x900",
            native_zoom=journey.native_zoom,
        ),
    )
