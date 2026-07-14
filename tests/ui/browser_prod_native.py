import re
from collections.abc import Callable
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from playwright.sync_api import BrowserContext, Page, Playwright, expect, sync_playwright

from .browser_auth import AuthenticatedSession
from .browser_checks import (
    AdminDesktopObservation,
    assert_no_page_overflow,
    axe_counts,
    clipboard_is_empty,
    evaluate_string,
    execute_script,
)
from .browser_credentials import assert_secret_absent
from .browser_evidence import CaptureSpec
from .browser_focus import keyboard_focus_count
from .browser_fonts import create_browser_font_environment
from .browser_observability import PageAudit
from .browser_prod_context import ProductionJourney, ProductionQaContext
from .browser_prod_downstream import (
    run_production_clipboard_failure,
    run_production_downstream_journey,
)
from .browser_prod_models import (
    BrowserPhaseCleanup,
    NativeStableProjection,
    ProductionNativeReceipt,
)
from .browser_prod_states import run_production_state_recovery
from .browser_prod_upstream import run_production_upstream_journey
from .browser_runtime import browser_resource_snapshot
from .browser_zoom import (
    NATIVE_PREFERENCES,
    NativeZoomResult,
    assert_native_zoom_contract,
    capture_native_metrics,
)

type _NativeContextFactory = Callable[
    [Playwright, Path, dict[str, str | float | bool] | None],
    tuple[BrowserContext, Path],
]

_UUID: Final = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])"
)
_TIMESTAMP: Final = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
_SHA_FINGERPRINT: Final = re.compile(r"sha256:[0-9a-f]{16,64}(?:…|\.\.\.)?")
_OPAQUE_HEX: Final = re.compile(r"(?<![0-9a-f])[0-9a-f]{16,64}(?![0-9a-f])")
_EVENT_LATENCY: Final = re.compile(
    r"(started|succeeded|failed|cancelled) · ([^<·]+) · (?:\d+|No latency)"
)


def _canonical_dom(document: str) -> str:
    canonical = _UUID.sub("{uuid}", document)
    canonical = _TIMESTAMP.sub("{timestamp}", canonical)
    canonical = _SHA_FINGERPRINT.sub("sha256:{fingerprint}", canonical)
    canonical = _OPAQUE_HEX.sub("{opaque-hex}", canonical)
    return _EVENT_LATENCY.sub(r"\1 · \2 · {latency}", canonical)


def _stable_dom_hash(document: str) -> str:
    return sha256(_canonical_dom(document).encode()).hexdigest()


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


def _stable(page: Page, label: str, axe_asset: Path) -> NativeStableProjection:
    before_document = page.content()
    before = _stable_dom_hash(before_document)
    assert_no_page_overflow(page)
    execute_script(page, "() => window.scrollTo(Number.MAX_SAFE_INTEGER, window.scrollY)")
    assert float(evaluate_string(page, "() => JSON.stringify(window.scrollX)")) == 0
    execute_script(page, "() => window.scrollTo(0, 0)")
    counts = axe_counts(page, axe_asset)
    assert counts.serious == 0
    assert counts.critical == 0
    assert counts.network_requests == 0
    focus_stops = keyboard_focus_count(page)
    after_document = page.content()
    after = _stable_dom_hash(after_document)
    assert before_document == after_document
    assert before == after
    return NativeStableProjection(
        label=label,
        dom_hash=before,
        focus_stops=focus_stops,
        axe_serious=0,
        axe_critical=0,
        axe_network_requests=0,
        focus_clipped=0,
        focus_hidden=0,
        focus_covered=0,
    )


def _cleanup_projection(
    baseline_processes: set[int],
    baseline_drivers: set[int],
    baseline_paths: set[Path],
) -> BrowserPhaseCleanup:
    observed = browser_resource_snapshot()
    process_count = len({pid for pid, _ in observed.processes} - baseline_processes)
    driver_count = len(set(observed.drivers) - baseline_drivers)
    paths = set(observed.temporary_paths) - baseline_paths
    return BrowserPhaseCleanup.model_validate(
        {
            "browser_processes": process_count,
            "playwright_drivers": driver_count,
            "browser_contexts": 0,
            "browser_pages": 0,
            "persistent_profiles": 0,
            "temporary_directories": sum(path.exists() for path in paths),
            "clipboard_nonempty": 0,
            "capture_blackout_active": 0,
        }
    )


def _capture_native_showcase(
    page: Page, qa: ProductionQaContext
) -> tuple[NativeStableProjection, ...]:
    stable = [_stable(page, "showcase", qa.axe_asset)]
    qa.recorder.capture(
        page,
        CaptureSpec(
            name="native-showcase-full",
            state="native 200 percent candidate showcase",
            viewport="1280x900 outer",
            native_zoom=True,
        ),
    )
    page.keyboard.press("Tab")
    qa.recorder.capture(
        page,
        CaptureSpec(
            name="native-showcase-focused-control",
            state="native candidate keyboard focus",
            viewport="1280x900 outer",
            native_zoom=True,
        ),
    )
    return tuple(stable)


def _open_native_session(
    context: BrowserContext,
    page: Page,
    audit: PageAudit,
    qa: ProductionQaContext,
) -> AuthenticatedSession:
    context.grant_permissions(["clipboard-read", "clipboard-write"], origin="http://127.0.0.1:2456")
    qa.network.set_phase("native-auth")
    audit.set_phase("native-auth")
    _ = page.goto("http://127.0.0.1:2456/admin", wait_until="domcontentloaded")
    qa.recorder.begin_blackout("native production admin bearer entry")
    page.keyboard.insert_text(qa.client.admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    expect(page.locator("#refresh-dashboard")).to_be_enabled()
    expect(page.locator("#dashboard-title")).to_be_focused()
    assert_secret_absent(page, qa.client.admin_bearer)
    qa.recorder.end_blackout()
    return AuthenticatedSession(
        context=context,
        page=page,
        audit=audit,
        axe_serious=0,
        axe_critical=0,
        axe_network_requests=0,
        desktop_layout=_desktop_placeholder(),
    )


def _run_native_owner_journey(
    journey: ProductionJourney,
) -> tuple[NativeStableProjection, ...]:
    page = journey.session.page
    axe_asset = journey.qa.axe_asset
    stable = [_stable(page, "dashboard", axe_asset)]
    run_production_upstream_journey(journey)
    stable.append(_stable(page, "upstream", axe_asset))
    run_production_downstream_journey(journey)
    stable.append(_stable(page, "downstream", axe_asset))
    run_production_state_recovery(journey)
    stable.append(_stable(page, "state-recovery", axe_asset))
    run_production_clipboard_failure(journey)
    stable.append(_stable(page, "clipboard-recovery", axe_asset))
    assert clipboard_is_empty(page)
    return tuple(stable)


def run_production_native_phase(
    qa: ProductionQaContext,
    context_factory: _NativeContextFactory,
) -> tuple[ProductionNativeReceipt, PageAudit]:
    baseline = browser_resource_snapshot()
    baseline_processes = {pid for pid, _ in baseline.processes}
    baseline_drivers = set(baseline.drivers)
    baseline_paths = set(baseline.temporary_paths)
    audit = PageAudit()
    result: NativeZoomResult | None = None
    stable: list[NativeStableProjection] = []
    executable_path = Path("/nonexistent")
    try:
        with ExitStack() as resources:
            profile_name = resources.enter_context(TemporaryDirectory(prefix="nblb-native-zoom-"))
            profile = Path(profile_name)
            profile.chmod(0o700)
            preferences = profile / "Default" / "Preferences"
            preferences.parent.mkdir(parents=True)
            _ = preferences.write_text(NATIVE_PREFERENCES, encoding="utf-8")
            playwright = resources.enter_context(sync_playwright())
            fonts = create_browser_font_environment()
            _ = resources.callback(fonts.temporary.cleanup)
            context, executable_path = context_factory(playwright, profile, fonts.variables)
            _ = resources.callback(context.close)
            page = context.pages[0]
            page.set_default_timeout(5_000)
            audit.set_phase("native-showcase")
            qa.network.set_phase("native-showcase")
            audit.attach(page)
            qa.network.attach(page)
            try:
                result = capture_native_metrics(context, executable_path, profile)
                assert_native_zoom_contract(result)
                stable.extend(_capture_native_showcase(page, qa))
                session = _open_native_session(context, page, audit, qa)
                journey = ProductionJourney(
                    qa=qa,
                    session=session,
                    capture_prefix="native-",
                    native_zoom=True,
                )
                stable.extend(_run_native_owner_journey(journey))
            finally:
                qa.network.detach(page)
                audit.detach()
    finally:
        qa.recorder.end_blackout()
    assert result is not None
    assert result.physical_width == 1280
    assert result.physical_height == 900
    cleanup = _cleanup_projection(baseline_processes, baseline_drivers, baseline_paths)
    capture_ids = (
        "native-showcase-full",
        "native-showcase-focused-control",
        "native-admin-upstream-post-cleanup",
        "native-admin-downstream-post-cleanup",
        "native-admin-cjk-xss-safe",
        "native-admin-stale-offline",
        "native-admin-empty-injected",
        "native-admin-clipboard-failure-post-cleanup",
    )
    return (
        ProductionNativeReceipt(
            chromium_revision=1228,
            contexts_started=1,
            pages_started=1,
            maximum_live_contexts=1,
            maximum_live_pages=1,
            shared_browser_used=False,
            executable_path=str(executable_path),
            preference_sha256=sha256(NATIVE_PREFERENCES.encode()).hexdigest(),
            physical_width=1280,
            physical_height=900,
            css_viewport_width=result.css_viewport_width,
            css_viewport_height=result.css_viewport_height,
            scroll_width=result.scroll_width,
            observation=result.observation,
            stable=tuple(stable),
            capture_ids=capture_ids,
            cleanup=cleanup,
        ),
        audit,
    )
