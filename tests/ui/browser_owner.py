from playwright.sync_api import Page, Route, expect

from .browser_auth import AuthenticatedSession
from .browser_checks import assert_no_page_overflow, clipboard_is_empty, evaluate_string
from .browser_credentials import assert_secret_absent, credential_state_observation
from .browser_evidence import CaptureSpec, EvidenceRecorder, ManualScenario
from .fake_admin_state import FakeAdminState

_UPSTREAM_CREDENTIAL = "synthetic-browser-upstream-custody-value"


def _exercise_escape_cancel(page: Page, recorder: EvidenceRecorder) -> None:
    invoker = page.locator("#add-upstream")
    cancel = page.locator("#upstream-dialog [data-close='upstream-dialog']")
    invoker.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-dialog")).to_be_visible()
    page.keyboard.insert_text(_UPSTREAM_CREDENTIAL)
    page.keyboard.press("Escape")
    expect(page.locator("#upstream-dialog")).to_be_hidden()
    expect(invoker).to_be_focused()
    assert_secret_absent(page, _UPSTREAM_CREDENTIAL)
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-dialog")).to_be_visible()
    page.keyboard.insert_text(_UPSTREAM_CREDENTIAL)
    page.keyboard.press("Tab")
    expect(cancel).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-dialog")).to_be_hidden()
    expect(invoker).to_be_focused()
    assert_secret_absent(page, _UPSTREAM_CREDENTIAL)
    recorder.add_scenario(
        ManualScenario(
            name="native dialog Escape cancel and resume",
            actions=(
                "#add-upstream Enter, enter a synthetic key, then Escape",
                "Reopen, enter the key again, Tab to Cancel, and press Enter",
            ),
            observables=(
                "Escape and Cancel reset the password control before focus restoration",
                "The same keyboard flow remains usable immediately after cancellation",
            ),
        )
    )


def _confirm_with_keyboard(page: Page) -> None:
    expect(page.locator("#confirm-dialog")).to_be_visible()
    assert page.locator(":focus").get_attribute("id") == "confirm-title"
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    assert page.locator(":focus").get_attribute("id") == "confirm-action"
    page.keyboard.press("Enter")
    expect(page.locator("#confirm-dialog")).to_be_hidden()


def _assert_focus_moves_to_first_available_next_row_action(page: Page) -> None:
    next_key = "key-00000000-0000-4000-8000-000000000002"
    expect(page.locator(f"#{next_key}-probe")).to_be_disabled()
    expect(page.locator(f"#{next_key}-toggle")).to_be_focused()


def _exercise_busy_probe(session: AuthenticatedSession, state: FakeAdminState) -> str:
    page = session.page
    busy_target = next(item for item in state.upstreams().items if not item.enabled)
    busy_probe_id = f"key-{busy_target.id}-probe"
    probe = page.locator(f"#{busy_probe_id}")
    pending_routes: list[Route] = []

    def hold_route(route: Route) -> None:
        pending_routes.append(route)

    probe_pattern = f"**/admin/api/v1/upstream-keys/{busy_target.id}/probe"
    _ = page.route(probe_pattern, hold_route)
    session.audit.set_phase("mutation_busy")
    probe.focus()
    page.keyboard.press("Enter")
    expect(probe).to_be_disabled()
    page.keyboard.press("Enter")
    assert len(pending_routes) == 1
    pending_routes[0].continue_()
    busy_row = page.locator("#upstream-body tr", has_text=str(busy_target.id))
    expect(busy_row.locator("td[data-label='Health']")).to_have_text(
        "Verified · Last check succeeded"
    )
    expect(probe).to_be_enabled()
    page.unroute(probe_pattern, hold_route)
    return busy_probe_id


def run_upstream_journey(
    session: AuthenticatedSession,
    recorder: EvidenceRecorder,
    state: FakeAdminState,
    capture_prefix: str = "",
    native_zoom: bool = False,
) -> None:
    page = session.page
    _exercise_escape_cancel(page, recorder)
    busy_probe_id = _exercise_busy_probe(session, state)
    session.audit.set_phase("upstream_journey")
    page.locator("#add-upstream").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-dialog")).to_be_visible()
    assert page.locator(":focus").get_attribute("id") == "upstream-key"
    recorder.begin_blackout("upstream credential in form control")
    page.keyboard.insert_text(_UPSTREAM_CREDENTIAL)
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-dialog")).to_be_hidden()
    assert_secret_absent(page, _UPSTREAM_CREDENTIAL)
    recorder.end_blackout()

    created = state.upstreams().items[-1]
    assert created.enabled is False
    assert page.locator(":focus").get_attribute("id") == "add-upstream"
    probe_id = f"key-{created.id}-probe"
    toggle_id = f"key-{created.id}-toggle"
    delete_id = f"key-{created.id}-delete"

    page.locator(f"#{probe_id}").focus()
    page.keyboard.press("Enter")
    created_row = page.locator("#upstream-body tr", has_text=str(created.id))
    expect(created_row.locator("td[data-label='Health']")).to_have_text(
        "Verified · Last check succeeded"
    )
    expect(page.locator(f"#{probe_id}")).to_be_focused()

    page.locator(f"#{toggle_id}").focus()
    page.keyboard.press("Enter")
    expect(page.locator(f"#{toggle_id}")).to_have_text("Disable")
    expect(page.locator(f"#{toggle_id}")).to_be_focused()
    expect(page.locator(f"#{delete_id}")).to_be_disabled()

    page.keyboard.press("Enter")
    if not native_zoom:
        recorder.capture(
            page,
            CaptureSpec(
                name="admin-destructive-confirmation-1280",
                state="disable-key confirmation at 1280px",
                viewport="1280x900",
            ),
        )
    _confirm_with_keyboard(page)
    expect(page.locator(f"#{toggle_id}")).to_have_text("Enable")
    expect(page.locator(f"#{toggle_id}")).to_be_focused()

    page.locator(f"#{delete_id}").focus()
    page.keyboard.press("Enter")
    _confirm_with_keyboard(page)
    expect(page.locator(f"#{toggle_id}")).to_have_count(0)
    _assert_focus_moves_to_first_available_next_row_action(page)
    recovery = next(
        item
        for item in state.upstreams().items
        if not item.enabled and item.health_state.value == "healthy"
    )
    recovery_toggle = page.locator(f"#key-{recovery.id}-toggle")
    recovery_toggle.click()
    expect(recovery_toggle).to_have_text("Disable")
    assert_no_page_overflow(page)
    recorder.capture(
        page,
        CaptureSpec(
            name=f"{capture_prefix}admin-upstream-post-cleanup",
            state="probe enable disable delete completed and eligible capacity restored",
            viewport="1280x900",
            native_zoom=native_zoom,
        ),
    )
    recorder.add_scenario(
        ManualScenario(
            name="synthetic upstream lifecycle",
            actions=(
                "#add-upstream Enter; #upstream-key keyboard input; Enter",
                f"Hold #{busy_probe_id}, press Enter twice, then release one request",
                f"#{probe_id} Enter; #{toggle_id} Enter",
                "#confirm-dialog keyboard disable and delete confirmations",
            ),
            observables=(
                "Creation is disabled and plaintext is absent after submit",
                "A pending mutation disables repeat submit and emits exactly one request",
                "Probe, enable, disable, and delete transitions are exact",
                "Deletion returns focus to the next row action",
            ),
        )
    )


def _select_one_time_credential(page: Page) -> None:
    page.keyboard.press("Tab")
    assert page.locator(":focus").get_attribute("id") == "one-time-token"
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
    page.keyboard.press("Tab")
    assert page.locator(":focus").get_attribute("id") == "copy-token"


def run_downstream_journey(
    session: AuthenticatedSession,
    recorder: EvidenceRecorder,
    state: FakeAdminState,
    capture_prefix: str = "",
    native_zoom: bool = False,
) -> None:
    page = session.page
    page.locator("#issue-downstream").focus()
    page.keyboard.press("Enter")
    assert page.locator(":focus").get_attribute("id") == "downstream-label"
    if not native_zoom:
        recorder.capture(
            page,
            CaptureSpec(
                name="admin-downstream-form-1280",
                state="empty downstream token form before label and scope entry",
                viewport="1280x900",
            ),
        )
    page.keyboard.insert_text("Browser owner journey")
    page.keyboard.press("Tab")
    page.keyboard.press("Space")
    page.keyboard.press("Tab")
    page.keyboard.press("Space")
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    recorder.begin_blackout("one-time downstream token response")
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_visible()
    assert page.locator(":focus").get_attribute("id") == "credential-title"
    expect(page.locator("#credential-title")).to_have_text(
        "Store credential for Browser owner journey"
    )
    expect(page.locator("#credential-target")).to_have_text(
        "Client Browser owner journey · Read models · Write chat"
    )
    state_before_copy = credential_state_observation(page)
    assert state_before_copy.admin_bearer_present
    assert state_before_copy.one_time_token_present
    assert state_before_copy.copied_credential is False
    _select_one_time_credential(page)
    page.keyboard.press("Enter")
    assert credential_state_observation(page).copied_credential
    page.keyboard.press("Tab")
    assert page.locator(":focus").get_attribute("id") == "dismiss-token"
    page.keyboard.press("Enter")
    expect(page.locator("#credential-dialog")).to_be_hidden()
    assert_secret_absent(page, state.issued_bearer)
    assert clipboard_is_empty(page)
    state_after_dismissal = credential_state_observation(page)
    assert state_after_dismissal.admin_bearer_present
    assert state_after_dismissal.one_time_token_present is False
    assert state_after_dismissal.copied_credential is False
    expect(page.locator("#credential-target")).to_be_empty()
    expect(page.locator("#credential-title")).to_have_text("Store this credential now")
    recorder.end_blackout()

    issued = next(item for item in state.tokens().items if item.label == "Browser owner journey")
    revoke_id = f"token-{issued.id}-revoke"
    page.locator(f"#{revoke_id}").focus()
    page.keyboard.press("Enter")
    _confirm_with_keyboard(page)
    expect(page.locator(f"#{revoke_id}")).to_contain_text("Revoked")
    assert page.locator(":focus").get_attribute("id") == revoke_id
    assert_no_page_overflow(page)
    recorder.capture(
        page,
        CaptureSpec(
            name=f"{capture_prefix}admin-downstream-post-cleanup",
            state="issue copy dismiss revoke completed",
            viewport="1280x900",
            native_zoom=native_zoom,
        ),
    )
    recorder.add_scenario(
        ManualScenario(
            name="one-time downstream token lifecycle",
            actions=(
                "#issue-downstream Enter and keyboard-only label/scope selection",
                "#copy-token Enter; #dismiss-token Enter in the same user gesture sequence",
                f"#{revoke_id} Enter and destructive confirmation",
            ),
            observables=(
                "Token is revealed once only inside the credential dialog",
                "Dismissal clears DOM, form state, module reference, and clipboard",
                "Revoked state is explicit and focus returns deterministically",
            ),
        )
    )
