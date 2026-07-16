import re
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from functools import partial

import pytest
from playwright.sync_api import APIResponse, BrowserContext, Error, Page, Request, Route, expect

from .browser_checks import evaluate_string, execute_script
from .browser_credentials import assert_secret_absent
from .browser_runtime import (
    UI_ORIGIN,
    RunningFakeServer,
    start_fake_server,
    start_managed_browser,
    stop_fake_server,
    stop_managed_browser,
)
from .fake_admin_state import FakeAdminState

pytestmark = pytest.mark.ui_fake

_DISABLED_ID = "00000000-0000-4000-8000-000000000001"
_ENABLED_ID = "00000000-0000-4000-8000-000000000002"
_DOWNSTREAM_ROW_ID = "00000000-0000-4000-8000-000000000003"
_UPSTREAM_FIXTURE = "synthetic-held-upstream-cancellation-value"


@pytest.fixture
def mutation_browser() -> Iterator[tuple[RunningFakeServer, BrowserContext, Page]]:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        yield server, context, page
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


@dataclass(frozen=True, slots=True)
class MutationCase:
    name: str
    path: str
    trigger: Callable[[Page], str]
    dialog_id: str | None = None


def _login(page: Page, state: FakeAdminState) -> None:
    if page.locator("#admin-bearer").count():
        page.locator("#admin-bearer").fill(state.admin_bearer)
        page.keyboard.press("Enter")
        page.locator("#dashboard-title").wait_for(state="visible")


def _refresh_fixture(page: Page, state: FakeAdminState, case: MutationCase | None = None) -> None:
    state.reset()
    _login(page, state)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
    toggle = page.locator(f"#key-{_DISABLED_ID}-toggle")
    expect(toggle).to_have_text("Enable")
    expect(toggle).to_be_disabled()
    if case is not None and case.name == "enable":
        page.locator(f"#key-{_DISABLED_ID}-probe").click()
        expect(toggle).to_be_enabled()
    if case is not None and case.name == "issue":
        page.locator(f"#key-{_DISABLED_ID}-probe").click()
        expect(toggle).to_be_enabled()
        toggle.click()
        expect(toggle).to_have_text("Disable")


def _projection(state: FakeAdminState) -> tuple[str, str]:
    return state.upstreams().model_dump_json(), state.tokens().model_dump_json()


def _click(page: Page, element_id: str) -> None:
    execute_script(page, f"() => document.getElementById('{element_id}').click()")


def _trigger_add(page: Page) -> str:
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill(_UPSTREAM_FIXTURE)
    page.locator("#submit-upstream").click()
    return "add-upstream"


def _trigger_issue(page: Page) -> str:
    page.locator("#issue-downstream").click()
    page.locator("#downstream-label").fill("Held issue cancellation")
    page.locator("input[name='scope']").first.check()
    page.locator("#submit-downstream").click()
    return "issue-downstream"


def _trigger_probe(page: Page) -> str:
    target = f"key-{_DISABLED_ID}-probe"
    page.locator(f"#{target}").click()
    return target


def _trigger_enable(page: Page) -> str:
    target = f"key-{_DISABLED_ID}-toggle"
    page.locator(f"#{target}").click()
    return target


def _trigger_confirm(page: Page, invoker: str) -> str:
    page.locator(f"#{invoker}").click()
    page.locator("#confirm-action").click()
    return invoker


_CASES = (
    MutationCase("add", "/admin/api/v1/upstream-keys", _trigger_add, "upstream-dialog"),
    MutationCase("issue", "/admin/api/v1/downstream-tokens", _trigger_issue, "downstream-dialog"),
    MutationCase("probe", f"/admin/api/v1/upstream-keys/{_DISABLED_ID}/probe", _trigger_probe),
    MutationCase("enable", f"/admin/api/v1/upstream-keys/{_DISABLED_ID}/enable", _trigger_enable),
    MutationCase(
        "disable",
        f"/admin/api/v1/upstream-keys/{_ENABLED_ID}/disable",
        lambda page: _trigger_confirm(page, f"key-{_ENABLED_ID}-toggle"),
        "confirm-dialog",
    ),
    MutationCase(
        "delete",
        f"/admin/api/v1/upstream-keys/{_DISABLED_ID}",
        lambda page: _trigger_confirm(page, f"key-{_DISABLED_ID}-delete"),
        "confirm-dialog",
    ),
    MutationCase(
        "revoke",
        f"/admin/api/v1/downstream-tokens/{_DOWNSTREAM_ROW_ID}",
        lambda page: _trigger_confirm(page, f"token-{_DOWNSTREAM_ROW_ID}-revoke"),
        "confirm-dialog",
    ),
)


def _settle(page: Page) -> None:
    script = """() => new Promise((resolve) =>
requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    execute_script(page, script)


def _hold_route(held: list[Route], route: Route) -> None:
    held.append(route)


def _request_matches(path: str, request: Request) -> bool:
    return request.url.endswith(path)


def _fulfill_problem(route: Route, status: int, code: str, message: str) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        json={
            "error": {
                "code": code,
                "message": message,
                "request_id": "synthetic-mutation-contract",
            }
        },
    )


def _assert_focus_inside_dialog(page: Page, dialog_id: str) -> None:
    observed = evaluate_string(
        page,
        "() => JSON.stringify(document.activeElement.closest('dialog')?.id ?? '')",
    )
    assert observed == f'"{dialog_id}"'


def _assert_bidirectional_focus_trap(page: Page, dialog_id: str) -> None:
    for key in ("Tab", "Shift+Tab"):
        for _ in range(4):
            page.keyboard.press(key)
            _assert_focus_inside_dialog(page, dialog_id)


def _cancel(page: Page, case: MutationCase, mode: str) -> None:
    if mode == "cancel":
        page.locator(f"#{case.dialog_id} [data-close='{case.dialog_id}']").click()
    elif mode == "escape":
        page.keyboard.press("Escape")
    elif mode == "logout":
        _click(page, "logout")
        page.locator("#admin-bearer").wait_for(state="visible")
    else:
        _ = page.reload(wait_until="domcontentloaded")
        page.locator("#admin-bearer").wait_for(state="visible")


def test_logout_and_reload_abort_unaccepted_mutations_without_late_ui_changes() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        for case in _CASES:
            modes = ("logout", "reload")
            for mode in modes:
                _refresh_fixture(page, server.state, case)
                before = _projection(server.state)
                held: list[Route] = []
                _ = page.route(f"**{case.path}", partial(_hold_route, held))
                with page.expect_request(partial(_request_matches, case.path)):
                    _ = case.trigger(page)
                expect(page.locator("[aria-busy='true']").last).to_be_visible()
                page.keyboard.press("Enter")
                assert len(held) == 1
                with page.expect_event(
                    "requestfailed",
                    predicate=partial(_request_matches, case.path),
                ):
                    _cancel(page, case, mode)
                _settle(page)
                stable_html = page.locator("body").inner_html()
                stable_focus = evaluate_string(
                    page,
                    "() => JSON.stringify(document.activeElement?.id ?? '')",
                )
                assert stable_focus != '""', (case.name, mode)
                with suppress(Error):
                    held[0].continue_()
                _settle(page)
                assert _projection(server.state) == before
                assert page.locator("body").inner_html() == stable_html
                assert (
                    evaluate_string(
                        page,
                        "() => JSON.stringify(document.activeElement?.id ?? '')",
                    )
                    == stable_focus
                )
                assert_secret_absent(page, _UPSTREAM_FIXTURE)
                _ = page.unroute(f"**{case.path}")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def _hold_committed_response(held: list[tuple[Route, APIResponse]], route: Route) -> None:
    held.append((route, route.fetch()))


def _pop_held_response(
    page: Page,
    held: list[tuple[Route, APIResponse]],
) -> tuple[Route, APIResponse]:
    for _ in range(100):
        if held:
            return held.pop()
        page.wait_for_timeout(10)
    detail = "intercepted response did not become available"
    raise AssertionError(detail)


def _commit_then_abort(route: Route) -> None:
    _ = route.fetch()
    route.abort()


def _assert_busy_dialog_contract(page: Page, case: MutationCase, dialog_id: str) -> None:
    dialog = page.locator(f"#{dialog_id}")
    expect(dialog).to_be_visible()
    expect(dialog.locator(f"[data-close='{dialog_id}']")).to_be_disabled()
    expect(dialog).to_have_attribute("aria-busy", "true")
    busy_status = dialog.locator("[data-dialog-busy]")
    expect(busy_status).to_be_visible()
    expect(busy_status).to_be_focused()
    if case.name == "add":
        expect(page.locator("#upstream-key")).to_be_disabled()
    if case.name == "issue":
        expect(page.locator("#downstream-label")).to_be_disabled()
        expect(page.locator("input[name='scope']").first).to_be_disabled()
    expect(dialog).to_have_attribute(
        "aria-describedby",
        re.compile(rf"(?:^| ){dialog_id}-busy(?: |$)"),
    )
    _assert_bidirectional_focus_trap(page, dialog_id)


@pytest.mark.parametrize("case", tuple(item for item in _CASES if item.dialog_id)[:3])
def test_busy_dialog_blocks_cancel_and_escape_until_committed_result_arrives(
    case: MutationCase,
) -> None:
    dialog_id = case.dialog_id
    assert dialog_id is not None
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _refresh_fixture(page, server.state, case)
        before = _projection(server.state)
        held: list[tuple[Route, APIResponse]] = []
        _ = page.route(f"**{case.path}", partial(_hold_committed_response, held))
        with page.expect_request(partial(_request_matches, case.path)):
            _ = case.trigger(page)
        _assert_busy_dialog_contract(page, case, dialog_id)
        page.keyboard.press("Escape")
        expect(page.locator(f"#{dialog_id}")).to_be_visible()
        assert len(held) == 1
        assert _projection(server.state) != before
        route, response = held[0]
        route.fulfill(response=response)
        if case.name == "issue":
            expect(page.locator("#credential-dialog")).to_be_visible()
            page.locator("#dismiss-token").click()
            expect(page.locator("#credential-dialog")).to_be_hidden()
        else:
            expect(page.locator(f"#{dialog_id}")).to_be_hidden()
        _ = page.unroute(f"**{case.path}")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_held_mutation_body_keeps_unknown_until_server_settles() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _refresh_fixture(page, server.state, _CASES[1])
        execute_script(
            page,
            """() => {
const nativeSetTimeout = window.setTimeout.bind(window);
window.setTimeout = (callback, delay, ...args) =>
  nativeSetTimeout(callback, delay === 130000 ? 25 : delay, ...args);
globalThis.__lateResponseClipboard = 'sentinel';
Object.defineProperty(navigator.clipboard, 'writeText', {
  configurable: true,
  value: async (value) => { globalThis.__lateResponseClipboard = value; },
});
}""",
        )
        held: list[tuple[Route, APIResponse]] = []
        path = "/admin/api/v1/downstream-tokens"
        _ = page.route(f"**{path}", partial(_hold_committed_response, held))
        _ = _trigger_issue(page)
        expect(page.locator("#downstream-error")).to_contain_text(
            "reached its deadline and may have completed"
        )
        expect(page.locator("#credential-dialog")).to_be_hidden()
        expect(page.locator("#one-time-token")).to_be_empty()
        route, response = _pop_held_response(page, held)
        stable_html = page.locator("body").inner_html()
        stable_focus = evaluate_string(
            page,
            "() => JSON.stringify(document.activeElement?.id ?? '')",
        )

        with suppress(Error):
            route.fulfill(response=response)
        page.wait_for_timeout(100)

        assert page.locator("body").inner_html() == stable_html
        assert (
            evaluate_string(page, "() => JSON.stringify(document.activeElement?.id ?? '')")
            == stable_focus
        )
        expect(page.locator("#credential-dialog")).to_be_hidden()
        expect(page.locator("#one-time-token")).to_be_empty()
        assert (
            evaluate_string(page, "() => JSON.stringify(globalThis.__lateResponseClipboard)")
            == '"sentinel"'
        )
        assert_secret_absent(page, server.state.issued_bearer)
        page.unroute(f"**{path}")
        page.locator("[data-close='downstream-dialog']").click()
        page.locator("#recommended-action").click()
        expect(page.locator("#last-result")).to_contain_text(
            "one-time credential response was lost"
        )
        expect(page.locator("#decision-title")).to_contain_text("Review unrecoverable token")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_settling_dashboard_keeps_unknown_and_only_refresh(
    mutation_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    _, _, page = mutation_browser
    probe_path = f"**/admin/api/v1/upstream-keys/{_DISABLED_ID}/probe"
    _ = page.route(probe_path, lambda route: route.abort(), times=1)
    page.locator(f"#key-{_DISABLED_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")

    _ = page.route(
        "**/admin/api/v1/dashboard",
        lambda route: _fulfill_problem(
            route,
            503,
            "admin_mutation_settling",
            "previous mutation is still settling",
        ),
        times=1,
    )
    page.locator("#retry-dashboard").click()
    expect(page.locator("#global-error-state")).to_have_text("Action still settling")
    expect(page.locator("#global-error-message")).to_contain_text("Do not repeat it")
    expect(page.locator("#retry-dashboard")).to_be_visible()
    expect(page.locator("#recommended-action")).to_be_hidden()
    assert page.locator("[data-mutation]:enabled").count() == 0

    page.locator("#retry-dashboard").click()
    expect(page.locator("#global-error")).to_be_hidden()
    expect(page.locator("#last-result")).to_contain_text("Fresh state for Key aaaaaaaa")


def test_mutation_401_and_capacity_rejection_are_known_no_success(
    mutation_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    server, _, page = mutation_browser
    probe_path = f"**/admin/api/v1/upstream-keys/{_DISABLED_ID}/probe"
    _ = page.route(
        probe_path,
        lambda route: _fulfill_problem(
            route,
            401,
            "admin_unauthorized",
            "admin authentication required",
        ),
        times=1,
    )
    page.locator(f"#key-{_DISABLED_ID}-probe").click()
    expect(page.locator("#login-error")).to_be_visible()
    expect(page.locator("#login-interrupted")).to_contain_text("Probing Key aaaaaaaa")
    page.locator("#admin-bearer").fill(server.state.admin_bearer)
    page.keyboard.press("Enter")
    expect(page.locator("#operation-status-line")).to_be_hidden()
    expect(page.locator("#last-result")).to_have_text("No action has completed in this tab.")

    _ = page.route(
        probe_path,
        lambda route: _fulfill_problem(
            route,
            503,
            "ledger_capacity_exhausted",
            "new mutation was not admitted",
        ),
        times=1,
    )
    page.locator(f"#key-{_DISABLED_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Action failed")
    expect(page.locator("#global-error-message")).to_contain_text(
        "service confirmed no successful result"
    )
    expect(page.locator("#operation-status-line")).to_be_hidden()
    expect(page.locator("#last-result")).to_have_text("No action has completed in this tab.")


@pytest.mark.parametrize("code", ["runtime_unavailable", "ledger_capacity_exhausted"])
def test_runtime_and_capacity_rejections_stale_snapshot_and_require_refresh(
    mutation_browser: tuple[RunningFakeServer, BrowserContext, Page],
    code: str,
) -> None:
    _, _, page = mutation_browser
    probe_path = f"**/admin/api/v1/upstream-keys/{_DISABLED_ID}/probe"
    _ = page.route(
        probe_path,
        lambda route: _fulfill_problem(route, 503, code, "mutation was not admitted"),
        times=1,
    )
    page.locator(f"#key-{_DISABLED_ID}-probe").click()
    expect(page.locator("#gateway-status")).to_contain_text("Stale")
    expect(page.locator("#retry-dashboard")).to_be_visible()
    expect(page.locator("#refresh-dashboard")).to_be_hidden()
    expect(page.locator("#recommended-action")).to_be_hidden()
    assert page.locator("[data-mutation]:enabled").count() == 0

    page.locator("#retry-dashboard").click()
    expect(page.locator("#global-error")).to_be_hidden()
    expect(page.locator("#gateway-status")).not_to_contain_text("Stale")
    expect(page.locator(f"#key-{_DISABLED_ID}-probe")).to_be_enabled()


def test_malformed_mutation_response_remains_unknown(
    mutation_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    _, _, page = mutation_browser
    probe_path = f"**/admin/api/v1/upstream-keys/{_DISABLED_ID}/probe"
    _ = page.route(
        probe_path,
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"unexpected":true}',
        ),
        times=1,
    )
    page.locator(f"#key-{_DISABLED_ID}-probe").click()
    expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
    expect(page.locator("#global-error-message")).to_contain_text("Invalid response")
    expect(page.locator(f"#result-{_DISABLED_ID}")).to_contain_text("result is unknown")
    expect(page.locator("#retry-dashboard")).to_be_visible()

    page.locator("#retry-dashboard").click()
    expect(page.locator("#last-result")).to_contain_text("Fresh state for Key aaaaaaaa")


def test_malformed_dashboard_response_uses_global_read_failure(
    mutation_browser: tuple[RunningFakeServer, BrowserContext, Page],
) -> None:
    _, _, page = mutation_browser
    _ = page.route(
        "**/admin/api/v1/dashboard",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"unexpected":true}',
        ),
        times=1,
    )
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#global-error-state")).to_have_text("Current state not confirmed")
    expect(page.locator("#global-error-message")).to_contain_text("Invalid response")
    expect(page.locator("#gateway-status")).to_contain_text("Stale")
    expect(page.locator("#retry-dashboard")).to_be_visible()

    page.locator("#retry-dashboard").click()
    expect(page.locator("#global-error")).to_be_hidden()


def test_login_refresh_and_probe_keep_visible_focus_while_requests_are_pending() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 375, "height": 812})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        held: list[tuple[Route, APIResponse]] = []
        _ = page.route("**/admin/api/v1/dashboard", partial(_hold_committed_response, held))
        page.locator("#admin-bearer").fill(server.state.admin_bearer)
        with page.expect_request(lambda request: request.url.endswith("/dashboard")):
            page.locator("#login-submit").click()
        expect(page.locator("#login-busy")).to_be_focused()
        route, response = _pop_held_response(page, held)
        route.fulfill(response=response)
        page.unroute("**/admin/api/v1/dashboard")
        expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")

        _ = page.route("**/admin/api/v1/dashboard", partial(_hold_committed_response, held))
        page.locator("#refresh-dashboard").click()
        expect(page.locator("#refresh-status")).to_be_focused()
        expect(page.locator("#refresh-dashboard")).to_have_text("Refreshing…")
        route, response = _pop_held_response(page, held)
        route.fulfill(response=response)
        page.unroute("**/admin/api/v1/dashboard")
        expect(page.locator("#refresh-dashboard")).to_be_focused()

        path = f"/admin/api/v1/upstream-keys/{_DISABLED_ID}/probe"
        _ = page.route(f"**{path}", partial(_hold_committed_response, held))
        page.locator(f"#key-{_DISABLED_ID}-probe").click()
        expect(page.locator(f"#result-{_DISABLED_ID}")).to_be_focused()
        expect(page.locator(f"#result-{_DISABLED_ID}")).to_contain_text("in progress")
        route, response = _pop_held_response(page, held)
        route.fulfill(response=response)
        page.unroute(f"**{path}")
        expect(page.locator(f"#key-{_DISABLED_ID}-probe")).to_be_focused()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_confirmed_mutation_remains_visible_when_followup_refresh_fails() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        _ = page.route("**/admin/api/v1/dashboard", lambda route: route.abort())
        page.locator(f"#key-{_DISABLED_ID}-probe").click()
        expect(page.locator("#global-error-state")).to_have_text("Snapshot refresh not confirmed")
        expect(page.locator("#global-confirmed-result")).to_contain_text(
            "Probe confirmed Key aaaaaaaa is valid"
        )
        expect(page.locator(f"#result-{_DISABLED_ID}")).to_contain_text(
            "Probe confirmed Key aaaaaaaa is valid"
        )
        expect(page.locator(f"#result-{_DISABLED_ID}")).not_to_contain_text("in progress")
        row = page.locator(f"#key-{_DISABLED_ID}-probe").locator("xpath=ancestor::tr")
        expect(row).to_contain_text("Last confirmed")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_lost_probe_is_not_promoted_when_recovery_refresh_also_fails() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 768, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    path = f"/admin/api/v1/upstream-keys/{_DISABLED_ID}/probe"
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        _ = page.route(f"**{path}", _commit_then_abort)
        page.locator(f"#key-{_DISABLED_ID}-probe").click()
        expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
        expect(page.locator(f"#result-{_DISABLED_ID}")).to_contain_text("Success was not assumed")
        page.unroute(f"**{path}", _commit_then_abort)

        _ = page.route("**/admin/api/v1/dashboard", lambda route: route.abort())
        page.locator("#retry-dashboard").click()
        expect(page.locator("#global-error-state")).to_have_text("Action not confirmed")
        expect(page.locator("#global-confirmed-result")).to_be_hidden()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        (_CASES[2], "Fresh state for Key aaaaaaaa is Verified"),
        (_CASES[3], "Fresh state confirms Key aaaaaaaa is enabled"),
        (_CASES[4], "is disabled"),
        (_CASES[5], "no longer exists"),
        (_CASES[6], "is revoked"),
    ],
)
def test_committed_lost_mutation_response_reconciles_from_fresh_state(
    case: MutationCase,
    expected: str,
) -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _refresh_fixture(page, server.state, case)
        _ = page.route(f"**{case.path}", _commit_then_abort)
        with page.expect_request(partial(_request_matches, case.path)):
            _ = case.trigger(page)
        if case.dialog_id:
            dialog = page.locator(f"#{case.dialog_id}")
            expect(dialog).to_have_attribute("aria-busy", "false")
            expect(dialog.locator(f"[data-close='{case.dialog_id}']")).to_be_enabled()
            dialog.locator(f"[data-close='{case.dialog_id}']").click()
        else:
            expect(page.locator("#global-error")).to_be_visible()
        page.unroute(f"**{case.path}", _commit_then_abort)
        recovery = page.locator("#retry-dashboard")
        if recovery.is_visible():
            recovery.click()
        else:
            page.locator("#recommended-action").click()
        expect(page.locator("#last-result")).to_contain_text(expected)
        expect(page.locator("#last-result")).not_to_contain_text("did not complete")
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_noncancelled_mutation_applies_once_and_restores_focus() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        page.locator(f"#key-{_DISABLED_ID}-probe").click()
        target = page.locator(f"#key-{_DISABLED_ID}-toggle")
        expect(target).to_be_enabled()
        target.focus()
        page.keyboard.press("Enter")
        page.keyboard.press("Enter")
        expect(target).to_have_text("Disable")
        expect(target).to_be_focused()
        item = next(item for item in server.state.upstreams().items if str(item.id) == _DISABLED_ID)
        assert item.enabled is True
        matching = [
            event
            for event in server.state.events().items
            if str(event.upstream_key_id) == _DISABLED_ID
            and event.event_type == "upstream_key_enabled"
        ]
        assert len(matching) == 1
        page.locator("#logout").focus()
        page.keyboard.press("Enter")
        expect(page.locator("#admin-bearer")).to_be_visible()
        expect(page.locator("#admin-bearer")).to_be_focused()
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)


def test_delete_confirmation_identifies_target_and_focuses_neighbor_first_action() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        page.locator(f"#key-{_DISABLED_ID}-delete").click()
        expect(page.locator("#confirm-description")).to_contain_text("Key aaaaaaaa · ID 00000001")
        page.locator("#confirm-action").click()
        expect(page.locator(f"#key-{_ENABLED_ID}-toggle")).to_be_focused()
        expect(page.locator(f"#key-{_ENABLED_ID}-toggle")).to_be_enabled()
        expect(page.locator("#activity-summary")).to_contain_text("Key aaaaaaaa was deleted")
        assert all(str(item.id) != _DISABLED_ID for item in server.state.upstreams().items)
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)
