from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .browser_checks import (
    AdminDesktopObservation,
    ReducedMotionObservation,
    ShowcaseDesktopObservation,
    ZoomObservation,
)
from .browser_evidence import BrowserProvenance, ManualScenario


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class BrowserNetworkProjection(_StrictModel):
    phase: str
    method: str
    path: str
    status: int | None
    failed: bool
    query_present: bool


class BrowserPhaseCleanup(_StrictModel):
    browser_processes: Literal[0]
    playwright_drivers: Literal[0]
    browser_contexts: Literal[0]
    browser_pages: Literal[0]
    persistent_profiles: Literal[0]
    temporary_directories: Literal[0]
    clipboard_nonempty: Literal[0]
    capture_blackout_active: Literal[0]


class NativeStableProjection(_StrictModel):
    label: str
    dom_hash: str
    focus_stops: int
    axe_serious: Literal[0]
    axe_critical: Literal[0]
    axe_network_requests: Literal[0]
    focus_clipped: Literal[0]
    focus_hidden: Literal[0]
    focus_covered: Literal[0]


class ProductionNativeReceipt(_StrictModel):
    chromium_revision: Literal[1228]
    contexts_started: Literal[1]
    pages_started: Literal[1]
    maximum_live_contexts: Literal[1]
    maximum_live_pages: Literal[1]
    shared_browser_used: Literal[False]
    executable_path: str
    preference_sha256: str
    physical_width: Literal[1280]
    physical_height: Literal[900]
    css_viewport_width: float
    css_viewport_height: float
    scroll_width: int
    observation: ZoomObservation
    stable: tuple[NativeStableProjection, ...]
    capture_ids: tuple[str, ...]
    cleanup: BrowserPhaseCleanup


class ProductionBrowserReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["PASS"] = "PASS"
    source_tree_sha256: str
    image_digest: str
    run_name: Literal["run-a", "run-b"]
    provenance: BrowserProvenance
    scenarios: tuple[ManualScenario, ...]
    axe_serious: Literal[0]
    axe_critical: Literal[0]
    axe_network_requests: Literal[0]
    console_errors: Literal[0]
    page_errors: Literal[0]
    reduced_motion: ReducedMotionObservation
    admin_desktop: AdminDesktopObservation
    showcase_desktop: ShowcaseDesktopObservation
    network: tuple[BrowserNetworkProjection, ...]
    network_query_count: Literal[0]
    network_header_body_count: Literal[0]
    admin_bearer_dom_absent: Literal[True]
    one_time_token_dom_absent: Literal[True]
    ordinary_cleanup: BrowserPhaseCleanup
    native: ProductionNativeReceipt


class ProductionRunCandidate(_StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["PASS"] = "PASS"
    source_tree_sha256: str
    image_digest: str
    run_name: Literal["run-a", "run-b"]
    capture_count: int
    ordinary_phase_first: Literal[True]
    native_phase_second: Literal[True]
    lighthouse_pending: Literal[True]
