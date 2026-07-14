from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.sync_api import BrowserContext, sync_playwright

from .browser_checks import clipboard_is_empty
from .browser_evidence import CleanupReceipt, EvidenceRecorder, ResourceObservation
from .browser_fonts import create_browser_font_environment
from .browser_native import NativeJourneyResult, run_native_owner_journey
from .browser_native_receipt import NativeDeterministicProjection, NativeZoomReceipt
from .browser_runtime import (
    BrowserResourceSnapshot,
    BrowserRuntimeError,
    RunningFakeServer,
    authority_is_free,
    browser_resource_snapshot,
    stop_fake_server,
)
from .browser_zoom import (
    NATIVE_PREFERENCES,
    NativeZoomResult,
    assert_native_zoom_contract,
    capture_native_metrics,
    start_native_headless_context,
)


@dataclass(frozen=True, slots=True)
class _Tracker:
    baseline: BrowserResourceSnapshot
    server: RunningFakeServer
    context: BrowserContext | None
    profile: Path


def _observation(
    tracker: _Tracker,
    clipboard_nonempty: int,
    blackout: bool,
    axe_network_requests: int,
) -> ResourceObservation:
    snapshot = browser_resource_snapshot()
    processes = {process for process, _ in snapshot.processes} - {
        process for process, _ in tracker.baseline.processes
    }
    drivers = set(snapshot.drivers) - set(tracker.baseline.drivers)
    paths = set(snapshot.temporary_paths) - set(tracker.baseline.temporary_paths)
    contexts = int(tracker.context is not None)
    pages = 0 if tracker.context is None else len(tracker.context.pages)
    return ResourceObservation(
        fake_server_threads=int(tracker.server.thread.is_alive()),
        port_2456_listeners=int(not authority_is_free()),
        browser_processes=len(processes),
        playwright_drivers=len(drivers),
        browser_contexts=contexts,
        browser_pages=pages,
        persistent_profiles=int(tracker.profile.exists()),
        temporary_directories=sum(path.exists() for path in paths),
        clipboard_nonempty=clipboard_nonempty,
        capture_blackout_active=int(blackout),
        axe_network_requests=axe_network_requests,
    )


def _cleanup_receipt(
    baseline: BrowserResourceSnapshot, observed: ResourceObservation
) -> CleanupReceipt:
    return CleanupReceipt.model_validate(
        {
            "port_2456_available_before": True,
            "browser_process_baseline": len(baseline.processes),
            "playwright_driver_baseline": len(baseline.drivers),
            "temporary_directory_baseline": len(baseline.temporary_paths),
            **observed.model_dump(),
        }
    )


def _deterministic(
    result: NativeZoomResult, journey: NativeJourneyResult
) -> NativeDeterministicProjection:
    return NativeDeterministicProjection(
        preference_sha256=result.preference_sha256,
        physical_width=result.physical_width,
        physical_height=result.physical_height,
        css_viewport_width=result.css_viewport_width,
        css_viewport_height=result.css_viewport_height,
        scroll_width=result.scroll_width,
        observation=result.observation,
        dom_hash_after=result.dom_hash_after,
        stable_dom_hashes=tuple(item.dom_hash for item in journey.stable),
        stable_focus_stops=tuple(item.focus_stops for item in journey.stable),
        stable_focus_clipped=tuple(item.focus_clipped for item in journey.stable),
        stable_focus_hidden=tuple(item.focus_hidden for item in journey.stable),
        stable_focus_covered=tuple(item.focus_covered for item in journey.stable),
        capture_ids=journey.capture_ids,
    )


def run_native_zoom_qa(
    recorder: EvidenceRecorder,
    server: RunningFakeServer,
    axe_asset: Path,
) -> NativeZoomReceipt:
    resources = ExitStack()
    baseline = browser_resource_snapshot()
    context: BrowserContext | None = None
    profile_path = Path("/nonexistent-native-profile")
    try:
        profile_name = resources.enter_context(TemporaryDirectory(prefix="nblb-native-zoom-"))
        profile_path = Path(profile_name)
        profile_path.chmod(0o700)
        preferences = profile_path / "Default" / "Preferences"
        preferences.parent.mkdir(parents=True)
        _ = preferences.write_text(NATIVE_PREFERENCES, encoding="utf-8")
        playwright = resources.enter_context(sync_playwright())
        font_environment = create_browser_font_environment()
        _ = resources.callback(font_environment.temporary.cleanup)
        context, executable_path = start_native_headless_context(
            playwright, profile_path, font_environment.variables
        )
        _ = resources.callback(context.close)
        result = capture_native_metrics(context, executable_path, profile_path)
        assert_native_zoom_contract(result)
        journey = run_native_owner_journey(
            context, context.pages[0], recorder, server.state, axe_asset
        )
        axe_requests = sum(item.axe_network_requests for item in journey.stable)
        before = _observation(
            _Tracker(baseline, server, context, profile_path),
            int(not clipboard_is_empty(context.pages[0])),
            recorder.blackout_active(),
            axe_requests,
        )
    finally:
        try:
            recorder.end_blackout()
        finally:
            try:
                resources.close()
            finally:
                stop_fake_server(server)
    after_observation = _observation(
        _Tracker(baseline, server, None, profile_path),
        0,
        recorder.blackout_active(),
        0,
    )
    after = _cleanup_receipt(baseline, after_observation)
    if before.browser_contexts != 1 or before.browser_pages != 1:
        reason = "native maximum live context or page observation changed"
        raise BrowserRuntimeError(reason)
    if before.persistent_profiles != 1:
        reason = "native profile was not directly observed before cleanup"
        raise BrowserRuntimeError(reason)
    return NativeZoomReceipt(
        deterministic=_deterministic(result, journey),
        executable_path=str(result.executable_path),
        chromium_revision=1228,
        contexts_started=1,
        pages_started=1,
        maximum_live_contexts=1,
        maximum_live_pages=1,
        shared_browser_used=False,
        browser_process_baseline=len(baseline.processes),
        playwright_driver_baseline=len(baseline.drivers),
        profile_path_present_before_cleanup=True,
        profile_paths_after_cleanup=after.persistent_profiles,
        browser_processes_after_cleanup=after.browser_processes,
        playwright_drivers_after_cleanup=after.playwright_drivers,
        cleanup_before=before,
        cleanup_after=after,
    )
