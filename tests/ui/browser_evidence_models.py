from typing import ClassVar, Literal, final, override

from pydantic import BaseModel, ConfigDict

from .browser_checks import (
    AdminDesktopObservation,
    ReducedMotionObservation,
    ShowcaseDesktopObservation,
)


class EvidenceModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class BrowserProvenance(EvidenceModel):
    playwright_version: Literal["1.61.0"]
    chromium_revision: Literal[1228]
    browser_version: str
    executable_path: str
    actual_process_executables: tuple[str, ...]
    channel_override: None = None
    executable_override: None = None
    download_performed: Literal[False] = False
    trace_enabled: Literal[False] = False
    har_enabled: Literal[False] = False
    video_enabled: Literal[False] = False
    retry_enabled: Literal[False] = False


class CaptureRecord(EvidenceModel):
    name: str
    route: str
    state: str
    viewport: str
    reduced_motion: bool
    native_zoom: bool
    path: str
    sha256: str
    byte_count: int
    source_newest_mtime_ns: int
    capture_mtime_ns: int
    source_paths: tuple[str, ...]
    pixel_width: int = 1
    pixel_height: int = 1
    landmarks: tuple[str, ...] = ()
    content_row_coverage: float = 0


class CaptureSpec(EvidenceModel):
    name: str
    state: str
    viewport: str
    reduced_motion: bool = False
    native_zoom: bool = False


class ManualScenario(EvidenceModel):
    name: str
    status: Literal["passed"] = "passed"
    actions: tuple[str, ...]
    observables: tuple[str, ...]


class AdversarialProbe(EvidenceModel):
    probe_class: str
    status: Literal["passed", "not_applicable"]
    observable: str


class ResourceObservation(EvidenceModel):
    fake_server_threads: int
    port_2456_listeners: int
    browser_processes: int
    playwright_drivers: int
    browser_contexts: int
    browser_pages: int
    persistent_profiles: int
    temporary_directories: int
    clipboard_nonempty: int
    capture_blackout_active: int
    axe_network_requests: int


class CleanupReceipt(EvidenceModel):
    port_2456_available_before: Literal[True]
    browser_process_baseline: int
    playwright_driver_baseline: int
    temporary_directory_baseline: int
    fake_server_threads: Literal[0]
    port_2456_listeners: Literal[0]
    browser_processes: Literal[0]
    playwright_drivers: Literal[0]
    browser_contexts: Literal[0]
    browser_pages: Literal[0]
    persistent_profiles: Literal[0]
    temporary_directories: Literal[0]
    clipboard_nonempty: Literal[0]
    capture_blackout_active: Literal[0]
    axe_network_requests: Literal[0]


class CleanupChronology(EvidenceModel):
    chronology: tuple[
        Literal["ordinary_before_cleanup"],
        Literal["ordinary_after_cleanup"],
        Literal["native_before_cleanup"],
        Literal["native_after_cleanup"],
    ]
    ordinary_before_cleanup: ResourceObservation
    ordinary_after_cleanup: CleanupReceipt
    native_before_cleanup: ResourceObservation
    native_after_cleanup: CleanupReceipt


class CaptureIndex(EvidenceModel):
    captures: tuple[CaptureRecord, ...]


class BrowserLogCount(EvidenceModel):
    phase: Literal["auth_wrong_token", "auth_initial_503", "offline_refresh"]
    source: Literal["network"]
    level: Literal["error"]
    count: Literal[1]


class ScrollPosition(EvidenceModel):
    x: float
    y: float


class BrowserObservabilityReceipt(EvidenceModel):
    application_console_errors: Literal[0]
    runtime_exceptions: Literal[0]
    page_errors: Literal[0]
    csp_or_javascript_errors: Literal[0]
    unexpected_browser_logs: Literal[0]
    user_agent_network_errors: Literal[3]
    wrong_auth_401_responses: Literal[1]
    initial_503_responses: Literal[1]
    offline_get_failed_requests: Literal[1]
    browser_request_events: int
    browser_response_events: int
    fake_server_response_events: int
    network_accounting_matches: Literal[True]
    expected_browser_logs: tuple[BrowserLogCount, BrowserLogCount, BrowserLogCount]


class ManualQaReceipt(EvidenceModel):
    provenance: BrowserProvenance
    scenarios: tuple[ManualScenario, ...]
    axe_serious: Literal[0]
    axe_critical: Literal[0]
    console_errors: Literal[0]
    page_errors: Literal[0]
    reduced_motion: ReducedMotionObservation
    admin_desktop: AdminDesktopObservation
    showcase_desktop: ShowcaseDesktopObservation
    observability: BrowserObservabilityReceipt
    telemetry_fields: tuple[str, ...] = ("method", "path", "status")
    telemetry_query_count: Literal[0] = 0
    telemetry_header_body_count: Literal[0] = 0


class AdversarialReceipt(EvidenceModel):
    probes: tuple[AdversarialProbe, ...]


@final
class CaptureBlockedError(Exception):
    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason


@final
class CaptureVerificationError(Exception):
    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason
