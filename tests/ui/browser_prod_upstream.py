import json
from collections.abc import Callable
from hashlib import sha256

from playwright.sync_api import Route, expect

from .browser_auth import AuthenticatedSession
from .browser_checks import assert_no_page_overflow
from .browser_credentials import assert_secret_absent, credential_state_observation
from .browser_evidence import CaptureSpec, ManualScenario
from .browser_prod_context import ProductionJourney


def _phase(journey: ProductionJourney, name: str) -> None:
    journey.session.audit.set_phase(name)
    journey.qa.network.set_phase(name)


def _synthetic_credential(purpose: str) -> str:
    digest = sha256(purpose.encode("ascii")).hexdigest()
    return f"nvapi-{digest}"


def _problem(
    status: int,
    code: str,
    message: str,
    request_id: str,
) -> Callable[[Route], None]:
    payload = json.dumps(
        {"error": {"code": code, "message": message, "request_id": request_id}},
        separators=(",", ":"),
    )

    def fulfill(route: Route) -> None:
        route.fulfill(status=status, content_type="application/json", body=payload)

    return fulfill


def _confirm(session: AuthenticatedSession) -> None:
    page = session.page
    expect(page.locator("#confirm-dialog")).to_be_visible()
    assert page.locator(":focus").get_attribute("id") == "confirm-title"
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    assert page.locator(":focus").get_attribute("id") == "confirm-action"
    page.keyboard.press("Enter")
    expect(page.locator("#confirm-dialog")).to_be_hidden()


def _cancel_and_escape(journey: ProductionJourney) -> None:
    page = journey.session.page
    invoker = page.locator("#add-upstream")
    credential = _synthetic_credential(
        f"browser-prod-cancel:{journey.qa.run_name}:{journey.capture_prefix}"
    )
    assert_secret_absent(page, credential)
    invoker.focus()
    page.keyboard.press("Enter")
    page.keyboard.insert_text(credential)
    page.keyboard.press("Escape")
    expect(page.locator("#upstream-dialog")).to_be_hidden()
    expect(invoker).to_be_focused()
    assert_secret_absent(page, credential)
    page.keyboard.press("Enter")
    page.keyboard.insert_text(credential)
    page.keyboard.press("Tab")
    expect(page.locator("#upstream-dialog [data-close='upstream-dialog']")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-dialog")).to_be_hidden()
    assert_secret_absent(page, credential)
    journey.qa.recorder.add_scenario(
        ManualScenario(
            name="production upstream Cancel and Escape custody",
            actions=("Enter synthetic key then Escape", "Reopen and activate Cancel"),
            observables=("Password control reset", "Focus restored to Add upstream key"),
        )
    )


def _probe_once(journey: ProductionJourney, item_id: str) -> tuple[str, str]:
    page = journey.session.page
    probe_id = f"key-{item_id}-probe"
    toggle_id = f"key-{item_id}-toggle"
    held: list[Route] = []

    def hold(route: Route) -> None:
        held.append(route)

    _phase(journey, f"{journey.capture_prefix}upstream_busy_probe")
    pattern = f"**/admin/api/v1/upstream-keys/{item_id}/probe"
    _ = page.route(pattern, hold)
    page.locator(f"#{probe_id}").focus()
    page.keyboard.press("Enter")
    expect(page.locator(f"#{probe_id}")).to_be_disabled()
    page.keyboard.press("Enter")
    assert len(held) == 1
    held[0].continue_()
    row = page.locator("#upstream-body tr", has_text=item_id)
    expect(row.locator("td[data-label='Health']")).to_have_text("healthy · success")
    expect(page.locator(f"#{probe_id}")).to_be_enabled()
    page.unroute(pattern, hold)
    return toggle_id, f"key-{item_id}-delete"


def _exercise_action_failures(journey: ProductionJourney, item_id: str) -> None:
    page = journey.session.page
    prefix = journey.capture_prefix
    probe_id = f"key-{item_id}-probe"
    toggle_id = f"key-{item_id}-toggle"

    _phase(journey, f"{prefix}upstream_probe_503")
    _ = page.route(
        f"**/admin/api/v1/upstream-keys/{item_id}/probe",
        _problem(
            503,
            "database_unavailable",
            "administration state unavailable",
            "browser-prod-probe-503",
        ),
        times=1,
    )
    page.locator(f"#{probe_id}").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#global-error")).to_be_visible()
    expect(page.locator("#stale-warning")).to_be_visible()
    expect(page.locator("#global-error")).to_be_focused()
    expect(page.locator("#global-error-message")).to_have_text(
        "database_unavailable · administration state unavailable · Request browser-prod-probe-503"
    )
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-action-probe-503",
            state="bounded probe 503 retains safe data and exact request identity",
            viewport="1280x900 outer" if journey.native_zoom else "1280x900",
            native_zoom=journey.native_zoom,
        ),
    )
    _phase(journey, f"{prefix}upstream_probe_503_recovery")
    page.locator("#retry-dashboard").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#global-error")).to_be_hidden()
    expect(page.locator("#stale-warning")).to_be_hidden()
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator(f"#{toggle_id}")).to_have_text("Enable")

    _phase(journey, f"{prefix}upstream_enable_401")
    _ = page.route(
        f"**/admin/api/v1/upstream-keys/{item_id}/enable",
        _problem(
            401,
            "admin_unauthorized",
            "admin authentication required",
            "browser-prod-enable-401",
        ),
        times=1,
    )
    page.locator(f"#{toggle_id}").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#login-error")).to_be_visible()
    expect(page.locator("#login-error")).to_be_focused()
    expect(page.locator("dialog[open]")).to_have_count(0)
    assert not any(credential_state_observation(page).model_dump().values())
    assert_secret_absent(page, journey.qa.client.admin_bearer)
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-action-enable-401",
            state="bounded enable 401 clears custody and requires reauthentication",
            viewport="1280x900 outer" if journey.native_zoom else "1280x900",
            native_zoom=journey.native_zoom,
        ),
    )
    _phase(journey, f"{prefix}upstream_enable_401_recovery")
    journey.qa.recorder.begin_blackout("admin bearer reentered after action 401")
    page.locator("#admin-bearer").focus()
    page.keyboard.insert_text(journey.qa.client.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator(f"#{toggle_id}")).to_have_text("Enable")
    assert_secret_absent(page, journey.qa.client.admin_bearer)
    journey.qa.recorder.end_blackout()


def _disable_and_delete(
    journey: ProductionJourney, item_id: str, toggle_id: str, delete_id: str
) -> None:
    page = journey.session.page
    _phase(journey, f"{journey.capture_prefix}upstream_enable")
    page.locator(f"#{toggle_id}").focus()
    page.keyboard.press("Enter")
    expect(page.locator(f"#{toggle_id}")).to_have_text("Disable")
    page.keyboard.press("Enter")
    _confirm(journey.session)
    expect(page.locator(f"#{toggle_id}")).to_have_text("Enable")
    page.locator(f"#{delete_id}").focus()
    page.keyboard.press("Enter")
    _confirm(journey.session)
    expect(page.locator(f"#{delete_id}")).to_have_count(0)
    focused = page.locator(":focus").get_attribute("id") or ""
    focus_is_valid = focused == "upstream-heading" or (
        focused.startswith("key-") and focused.endswith("-toggle")
    )
    assert focus_is_valid
    assert item_id not in {str(item.id) for item in journey.qa.client.upstreams().items}


def run_production_upstream_journey(journey: ProductionJourney) -> None:
    page = journey.session.page
    prefix = journey.capture_prefix
    _phase(journey, f"{prefix}upstream_cancel")
    _cancel_and_escape(journey)
    before_ids = {item.id for item in journey.qa.client.upstreams().items}
    credential = _synthetic_credential(
        f"browser-prod-create:{journey.qa.run_name}:{prefix or 'ordinary'}"
    )
    assert_secret_absent(page, credential)
    _phase(journey, f"{prefix}upstream_create")
    page.locator("#add-upstream").focus()
    page.keyboard.press("Enter")
    journey.qa.recorder.begin_blackout("synthetic upstream credential in production form")
    page.keyboard.insert_text(credential)
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-dialog")).to_be_hidden()
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#add-upstream")).to_be_focused()
    assert_secret_absent(page, credential)
    journey.qa.recorder.end_blackout()
    created = next(
        item for item in journey.qa.client.upstreams().items if item.id not in before_ids
    )
    _exercise_action_failures(journey, str(created.id))
    toggle_id, delete_id = _probe_once(journey, str(created.id))
    _disable_and_delete(journey, str(created.id), toggle_id, delete_id)
    assert_no_page_overflow(page)
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-upstream-post-cleanup",
            state="actual add probe enable disable delete completed",
            viewport="1280x900 outer" if journey.native_zoom else "1280x900",
            native_zoom=journey.native_zoom,
        ),
    )
    journey.qa.recorder.add_scenario(
        ManualScenario(
            name=f"{prefix or 'ordinary '}production upstream lifecycle",
            actions=(
                "Actual UI add",
                "Inject probe 503 and enable 401",
                "Reauthenticate then hold one probe",
                "Enable disable delete",
            ),
            observables=(
                "Exact safe problem identity remains visible with stale data",
                "Action 401 clears credential and dialog custody",
                "One mutation and safe actual API states",
                "Synthetic key absent",
            ),
        )
    )
