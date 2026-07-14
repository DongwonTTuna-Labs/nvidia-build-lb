from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict

from .browser_checks import ZoomObservation
from .browser_evidence import CleanupReceipt, ResourceObservation


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class NativeDeterministicProjection(_StrictModel):
    preference_sha256: str
    physical_width: int
    physical_height: int
    css_viewport_width: float
    css_viewport_height: float
    scroll_width: int
    observation: ZoomObservation
    dom_hash_after: str
    stable_dom_hashes: tuple[str, ...]
    stable_focus_stops: tuple[int, ...]
    stable_focus_clipped: tuple[int, ...]
    stable_focus_hidden: tuple[int, ...]
    stable_focus_covered: tuple[int, ...]
    capture_ids: tuple[str, ...]


class NativeZoomReceipt(_StrictModel):
    deterministic: NativeDeterministicProjection
    executable_path: str
    chromium_revision: Literal[1228]
    contexts_started: Literal[1]
    pages_started: Literal[1]
    maximum_live_contexts: Literal[1]
    maximum_live_pages: Literal[1]
    shared_browser_used: Literal[False]
    browser_process_baseline: int
    playwright_driver_baseline: int
    profile_path_present_before_cleanup: Literal[True]
    profile_paths_after_cleanup: Literal[0]
    browser_processes_after_cleanup: Literal[0]
    playwright_drivers_after_cleanup: Literal[0]
    cleanup_before: ResourceObservation
    cleanup_after: CleanupReceipt
