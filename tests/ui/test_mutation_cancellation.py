from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial

import pytest
from playwright.sync_api import Error, Page, Request, Route, expect

from .browser_checks import execute_script
from .browser_credentials import assert_secret_absent
from .browser_runtime import (
    UI_ORIGIN,
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


def _refresh_fixture(page: Page, state: FakeAdminState) -> None:
    state.reset()
    _login(page, state)
    page.locator("#refresh-dashboard").click()
    expect(page.locator("#overview")).to_have_attribute("aria-busy", "false")
    expect(page.locator(f"#key-{_DISABLED_ID}-toggle")).to_have_text("Enable")


def _projection(state: FakeAdminState) -> tuple[str, str]:
    return state.upstreams().model_dump_json(), state.tokens().model_dump_json()


def _click(page: Page, element_id: str) -> None:
    execute_script(page, f"() => document.getElementById('{element_id}').click()")


def _trigger_add(page: Page) -> str:
    page.locator("#add-upstream").click()
    page.locator("#upstream-key").fill(_UPSTREAM_FIXTURE)
    _click(page, "submit-upstream")
    return "add-upstream"


def _trigger_issue(page: Page) -> str:
    page.locator("#issue-downstream").click()
    page.locator("#downstream-label").fill("Held issue cancellation")
    page.locator("input[name='scope']").first.check()
    _click(page, "submit-downstream")
    return "issue-downstream"


def _trigger_probe(page: Page) -> str:
    target = f"key-{_DISABLED_ID}-probe"
    _click(page, target)
    return target


def _trigger_enable(page: Page) -> str:
    target = f"key-{_DISABLED_ID}-toggle"
    _click(page, target)
    return target


def _trigger_confirm(page: Page, invoker: str) -> str:
    _click(page, invoker)
    _click(page, "confirm-action")
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


def test_held_mutations_cancel_without_late_state_refresh_or_focus() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.set_default_timeout(5_000)
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        for case in _CASES:
            modes = (
                ("cancel", "escape", "logout", "reload") if case.dialog_id else ("logout", "reload")
            )
            for mode in modes:
                _refresh_fixture(page, server.state)
                before = _projection(server.state)
                held: list[Route] = []
                _ = page.route(f"**{case.path}", partial(_hold_route, held))
                with page.expect_request(partial(_request_matches, case.path)):
                    invoker = case.trigger(page)
                expect(page.locator("[aria-busy='true']").last).to_be_visible()
                page.keyboard.press("Enter")
                assert len(held) == 1
                with page.expect_event(
                    "requestfailed",
                    predicate=partial(_request_matches, case.path),
                ):
                    _cancel(page, case, mode)
                if mode in {"cancel", "escape"}:
                    expect(page.locator(f"#{case.dialog_id}")).to_be_hidden()
                    expect(page.locator(f"#{invoker}")).to_be_focused()
                stable_html = page.locator("body").inner_html()
                stable_focus = page.locator(":focus").get_attribute("id")
                with suppress(Error):
                    held[0].continue_()
                _settle(page)
                assert _projection(server.state) == before
                assert page.locator("body").inner_html() == stable_html
                assert page.locator(":focus").get_attribute("id") == stable_focus
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


def test_noncancelled_mutation_applies_once_and_restores_focus() -> None:
    server = start_fake_server()
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        _login(page, server.state)
        target = page.locator(f"#key-{_DISABLED_ID}-toggle")
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
    finally:
        try:
            context.close()
        finally:
            try:
                stop_managed_browser(managed)
            finally:
                stop_fake_server(server)
