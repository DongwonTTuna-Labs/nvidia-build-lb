import json
from collections.abc import Callable, Mapping

from playwright.sync_api import Route, expect

from .browser_auth import AuthenticatedSession
from .browser_checks import (
    assert_no_page_overflow,
    execute_script,
    focused_id,
    storage_observation,
)
from .browser_credentials import assert_secret_absent, credential_state_observation
from .browser_evidence import CaptureSpec, ManualScenario
from .browser_prod_context import ProductionJourney
from .browser_runtime import UI_ORIGIN

_OVERVIEW = f"{UI_ORIGIN}/admin/api/v1/overview"


def _phase(journey: ProductionJourney, name: str) -> None:
    journey.session.audit.set_phase(name)
    journey.qa.network.set_phase(name)


def _abort(route: Route) -> None:
    route.abort()


def _fulfill(body: Mapping[str, object]) -> Callable[[Route], None]:
    payload = json.dumps(body, separators=(",", ":"))

    def handler(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=payload)

    return handler


def _install_empty_projection(session: AuthenticatedSession) -> None:
    page = session.page
    empty: dict[str, object] = {"items": []}
    overview: dict[str, object] = {
        "status": "degraded",
        "ready": False,
        "upstream_keys": {"total": 0, "enabled": 0, "eligible": 0, "cooling": 0, "degraded": 0},
        "downstream_tokens": {"total": 0, "active": 0, "revoked": 0},
        "request_count": 0,
        "last_event_at": None,
        "generated_at": "2026-01-01T00:00:00Z",
    }
    routes: dict[str, Mapping[str, object]] = {
        "/overview": overview,
        "/upstream-keys": empty,
        "/downstream-tokens": empty,
        "/events": empty,
    }
    for suffix, body in routes.items():
        _ = page.route(f"{UI_ORIGIN}/admin/api/v1{suffix}", _fulfill(body), times=1)


def run_production_state_recovery(journey: ProductionJourney) -> None:
    page = journey.session.page
    prefix = journey.capture_prefix
    _phase(journey, f"{prefix}offline_refresh")
    _ = page.route(_OVERVIEW, _abort, times=1)
    page.locator("#refresh-dashboard").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#stale-warning")).to_be_visible()
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#refresh-dashboard")).to_be_focused()
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-stale-offline",
            state="actual candidate data retained after bounded offline refresh",
            viewport="1280x900 outer" if journey.native_zoom else "1280x900",
            native_zoom=journey.native_zoom,
        ),
    )
    _phase(journey, f"{prefix}offline_recovery")
    page.keyboard.press("Enter")
    expect(page.locator("#stale-warning")).to_be_hidden()
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#refresh-dashboard")).to_be_focused()
    _phase(journey, f"{prefix}empty_projection")
    _install_empty_projection(journey.session)
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-body")).to_contain_text("No upstream keys registered")
    expect(page.locator("#downstream-body")).to_contain_text("No downstream tokens issued")
    expect(page.locator("#events-body")).to_contain_text("No recent events")
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#refresh-dashboard")).to_be_focused()
    assert_no_page_overflow(page)
    journey.qa.recorder.capture(
        page,
        CaptureSpec(
            name=f"{prefix}admin-empty-injected",
            state="safe deterministic empty projection after real CRUD",
            viewport="1280x900 outer" if journey.native_zoom else "1280x900",
            native_zoom=journey.native_zoom,
        ),
    )
    _phase(journey, f"{prefix}empty_recovery")
    page.keyboard.press("Enter")
    expect(page.locator("#upstream-body")).not_to_contain_text("No upstream keys registered")
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#refresh-dashboard")).to_be_focused()
    journey.qa.recorder.add_scenario(
        ManualScenario(
            name=f"{prefix or 'ordinary '}production state recovery",
            actions=("Abort one overview", "Recover", "Inject one empty safe projection"),
            observables=("Stale data retained", "Actual data restored", "No horizontal overflow"),
        )
    )


def exercise_production_reload_logout(journey: ProductionJourney) -> None:
    page = journey.session.page
    _phase(journey, "reload_clears_bearer")
    _ = page.reload(wait_until="domcontentloaded")
    page.locator("#admin-bearer").wait_for(state="visible")
    assert focused_id(page) == "admin-bearer"
    assert_secret_absent(page, journey.qa.client.admin_bearer)
    journey.qa.recorder.begin_blackout("admin bearer entered for production logout proof")
    page.keyboard.insert_text(journey.qa.client.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#dashboard-title")).to_be_focused()
    journey.qa.recorder.end_blackout()
    _phase(journey, "logout_clears_open_secret_form")
    page.locator("#add-upstream").focus()
    page.keyboard.press("Enter")
    synthetic_upstream_value = "nvapi-synthetic-browser-prod-logout"
    journey.qa.recorder.begin_blackout("synthetic upstream key entered before logout")
    page.keyboard.insert_text(synthetic_upstream_value)
    execute_script(page, "() => document.querySelector('#logout').click()")
    page.locator("#admin-bearer").wait_for(state="visible")
    assert_secret_absent(page, synthetic_upstream_value)
    assert not any(credential_state_observation(page).model_dump().values())
    journey.qa.recorder.end_blackout()
    journey.qa.recorder.begin_blackout("admin bearer entered for keyboard logout")
    page.keyboard.insert_text(journey.qa.client.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#dashboard-title")).to_be_focused()
    journey.qa.recorder.end_blackout()
    page.locator("#logout").focus()
    page.keyboard.press("Enter")
    page.locator("#admin-bearer").wait_for(state="visible")
    assert focused_id(page) == "admin-bearer"
    storage = storage_observation(page)
    assert storage.local == storage.session == storage.cookies == storage.query == 0
