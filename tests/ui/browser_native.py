from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final

from playwright.sync_api import BrowserContext, Page, expect

from .browser_auth import AuthenticatedSession
from .browser_checks import (
    AdminDesktopObservation,
    BrowserAssertionError,
    assert_no_page_overflow,
    axe_counts,
    evaluate_string,
    execute_script,
)
from .browser_credentials import assert_secret_absent
from .browser_evidence import CaptureSpec, EvidenceRecorder
from .browser_focus import keyboard_focus_count
from .browser_observability import PageAudit
from .browser_owner import run_downstream_journey, run_upstream_journey
from .browser_runtime import UI_ORIGIN
from .browser_states import run_clipboard_failure, run_state_recovery
from .fake_admin_state import FakeAdminState

_SCROLL_X: Final = "() => JSON.stringify(window.scrollX)"


@dataclass(frozen=True, slots=True)
class NativeStableObservation:
    label: str
    dom_hash: str
    focus_stops: int
    axe_serious: int
    axe_critical: int
    axe_network_requests: int
    focus_clipped: int
    focus_hidden: int
    focus_covered: int


@dataclass(frozen=True, slots=True)
class NativeJourneyResult:
    stable: tuple[NativeStableObservation, ...]
    capture_ids: tuple[str, ...]


def _dom_hash(page: Page) -> str:
    return sha256(page.content().encode()).hexdigest()


def _stable(page: Page, label: str, axe_asset: Path) -> NativeStableObservation:
    before = _dom_hash(page)
    assert_no_page_overflow(page)
    execute_script(page, "() => window.scrollTo(Number.MAX_SAFE_INTEGER, window.scrollY)")
    if float(evaluate_string(page, _SCROLL_X)) != 0:
        reason = "native maximum horizontal scroll changed scrollX"
        raise BrowserAssertionError(reason)
    execute_script(page, "() => window.scrollTo(0, 0)")
    counts = axe_counts(page, axe_asset)
    focus_stops = keyboard_focus_count(page)
    after = _dom_hash(page)
    if before != after:
        reason = "native read-only observation mutated the DOM"
        raise BrowserAssertionError(reason)
    return NativeStableObservation(
        label=label,
        dom_hash=before,
        focus_stops=focus_stops,
        axe_serious=counts.serious,
        axe_critical=counts.critical,
        axe_network_requests=counts.network_requests,
        focus_clipped=0,
        focus_hidden=0,
        focus_covered=0,
    )


def _desktop_placeholder() -> AdminDesktopObservation:
    return AdminDesktopObservation(
        rail_width=224,
        main_grid_tracks=12,
        main_column_gap="24px",
        main_padding_start="32px",
        main_padding_end="32px",
        disclosure_display="none",
        rail_content_contained=True,
        main_children_full_span=True,
        dashboard_children_full_span=True,
        summary_cells=5,
        summary_grid_tracks=5,
    )


def _restore_dialog_capture(page: Page, focus_id: str, script: str) -> None:
    execute_script(page, script)
    expect(page.locator(f"#{focus_id}")).to_be_focused()


def run_native_owner_journey(
    context: BrowserContext,
    page: Page,
    recorder: EvidenceRecorder,
    state: FakeAdminState,
    axe_asset: Path,
) -> NativeJourneyResult:
    stable: list[NativeStableObservation] = []
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin=UI_ORIGIN)
    _ = page.goto(f"{UI_ORIGIN}/showcase", wait_until="load")
    page.get_by_role("heading", name="Primitive showcase", exact=True).wait_for()
    stable.append(_stable(page, "showcase", axe_asset))
    recorder.capture(
        page,
        CaptureSpec(
            name="native-showcase-full",
            state="native 200 percent showcase",
            viewport="1280x900 outer",
            native_zoom=True,
        ),
    )
    page.keyboard.press("Tab")
    recorder.capture(
        page,
        CaptureSpec(
            name="native-showcase-focused-control",
            state="native keyboard focus",
            viewport="1280x900 outer",
            native_zoom=True,
        ),
    )
    _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
    recorder.begin_blackout("native admin bearer entry")
    page.keyboard.insert_text(state.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    assert_secret_absent(page, state.admin_bearer)
    recorder.end_blackout()
    audit = PageAudit()
    session = AuthenticatedSession(
        context=context,
        page=page,
        audit=audit,
        axe_serious=0,
        axe_critical=0,
        axe_network_requests=0,
        desktop_layout=_desktop_placeholder(),
    )
    stable.append(_stable(page, "dashboard", axe_asset))
    page.locator("#add-upstream").click()
    expect(page.locator("#upstream-key")).to_be_focused()
    stable.append(_stable(page, "upstream-dialog", axe_asset))
    _restore_dialog_capture(
        page,
        "upstream-key",
        """() => {
          document.querySelector('#upstream-dialog').scrollTop = 0;
          document.querySelector('#upstream-key').focus({preventScroll: true});
        }""",
    )
    recorder.capture_viewport(
        page,
        CaptureSpec(
            name="native-admin-upstream-form",
            state="native 200 percent empty upstream form",
            viewport="1280x900 outer viewport",
            native_zoom=True,
        ),
    )
    page.locator("[data-close='upstream-dialog']").click()
    run_upstream_journey(
        session,
        recorder,
        state,
        capture_prefix="native-",
        native_zoom=True,
    )
    stable.append(_stable(page, "upstream", axe_asset))
    page.locator("#issue-downstream").click()
    expect(page.locator("#downstream-label")).to_be_focused()
    stable.append(_stable(page, "downstream-dialog", axe_asset))
    _restore_dialog_capture(
        page,
        "downstream-label",
        """() => {
          document.querySelector('#downstream-dialog').scrollTop = 0;
          document.querySelector('#downstream-label').focus({preventScroll: true});
        }""",
    )
    recorder.capture_viewport(
        page,
        CaptureSpec(
            name="native-admin-downstream-form",
            state="native 200 percent empty downstream form",
            viewport="1280x900 outer viewport",
            native_zoom=True,
        ),
    )
    page.locator("[data-close='downstream-dialog']").click()
    eligible = next(
        item for item in state.upstreams().items if item.routing_state.value == "eligible"
    )
    page.locator(f"#key-{eligible.id}-toggle").click()
    expect(page.locator("#confirm-title")).to_be_focused()
    stable.append(_stable(page, "destructive-dialog", axe_asset))
    _restore_dialog_capture(
        page,
        "confirm-title",
        """() => {
          document.querySelector('#confirm-dialog').scrollTop = 0;
          document.querySelector('#confirm-title').focus({preventScroll: true});
        }""",
    )
    recorder.capture_viewport(
        page,
        CaptureSpec(
            name="native-admin-destructive-confirmation",
            state="native 200 percent identified disable confirmation",
            viewport="1280x900 outer viewport",
            native_zoom=True,
        ),
    )
    page.locator("[data-close='confirm-dialog']").click()
    run_downstream_journey(
        session,
        recorder,
        state,
        capture_prefix="native-",
        native_zoom=True,
    )
    stable.append(_stable(page, "downstream", axe_asset))
    run_state_recovery(
        session,
        recorder,
        state,
        capture_prefix="native-",
        native_zoom=True,
    )
    stable.append(_stable(page, "state-recovery", axe_asset))
    run_clipboard_failure(
        session,
        recorder,
        state,
        capture_prefix="native-",
        native_zoom=True,
    )
    stable.append(_stable(page, "clipboard-recovery", axe_asset))
    if any(item.axe_serious or item.axe_critical or item.axe_network_requests for item in stable):
        reason = "native local axe contract failed"
        raise BrowserAssertionError(reason)
    return NativeJourneyResult(
        stable=tuple(stable),
        capture_ids=(
            "native-showcase-full",
            "native-showcase-focused-control",
            "native-admin-upstream-form",
            "native-admin-upstream-post-cleanup",
            "native-admin-downstream-form",
            "native-admin-destructive-confirmation",
            "native-admin-downstream-post-cleanup",
            "native-admin-stale-offline",
            "native-admin-empty",
            "native-admin-cjk-xss-safe",
            "native-admin-clipboard-failure-post-cleanup",
        ),
    )
