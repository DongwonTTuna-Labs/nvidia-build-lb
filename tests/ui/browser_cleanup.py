from collections.abc import Callable
from typing import final

from playwright.sync_api import Error

from .browser_auth import AuthenticatedSession, close_authenticated_session
from .browser_checks import clipboard_is_empty, execute_script
from .browser_evidence import CleanupReceipt, EvidenceRecorder, ResourceObservation
from .browser_runtime import (
    UI_ORIGIN,
    ManagedBrowserSession,
    RunningFakeServer,
    authority_is_free,
    managed_driver_count,
    managed_process_count,
    managed_temporary_path_count,
    stop_fake_server,
    stop_managed_browser,
)


@final
class BrowserResources:
    __slots__ = (
        "authenticated",
        "axe_network_requests",
        "clipboard_clean",
        "managed",
        "port_available_before",
        "post_cleanup",
        "pre_cleanup",
        "server",
    )

    def __init__(self) -> None:
        super().__init__()
        self.server: RunningFakeServer | None = None
        self.port_available_before = authority_is_free()
        self.managed: ManagedBrowserSession | None = None
        self.authenticated: AuthenticatedSession | None = None
        self.axe_network_requests = 0
        self.clipboard_clean = True
        self.pre_cleanup: ResourceObservation | None = None
        self.post_cleanup: CleanupReceipt | None = None


def _observe_cleanup(
    resources: BrowserResources,
    capture_blackout_active: bool = False,
) -> ResourceObservation:
    managed = resources.managed
    server = resources.server
    contexts = () if managed is None else tuple(managed.browser.contexts)
    temporary_paths = () if managed is None else managed.temporary_paths
    return ResourceObservation.model_validate(
        {
            "fake_server_threads": 0 if server is None else int(server.thread.is_alive()),
            "port_2456_listeners": int(not authority_is_free()),
            "browser_processes": 0 if managed is None else managed_process_count(managed),
            "playwright_drivers": 0 if managed is None else managed_driver_count(managed),
            "browser_contexts": len(contexts),
            "browser_pages": sum(len(context.pages) for context in contexts),
            "persistent_profiles": sum(
                path.name.startswith("nblb-native-zoom-") and path.exists()
                for path in temporary_paths
            ),
            "temporary_directories": (
                0 if managed is None else managed_temporary_path_count(managed)
            ),
            "clipboard_nonempty": int(not resources.clipboard_clean),
            "capture_blackout_active": int(capture_blackout_active),
            "axe_network_requests": resources.axe_network_requests,
        }
    )


def assert_native_phase_boundary(resources: BrowserResources) -> None:
    observed = resources.post_cleanup or _observe_cleanup(resources)
    if any(
        (
            observed.browser_processes,
            observed.playwright_drivers,
            observed.browser_contexts,
            observed.browser_pages,
        )
    ):
        reason = "ordinary browser resources remain before native phase"
        raise Error(reason)


def cleanup_browser_resources(
    resources: BrowserResources,
    recorder: EvidenceRecorder,
    close_authenticated: Callable[[AuthenticatedSession], None] = close_authenticated_session,
) -> CleanupReceipt:
    authenticated = resources.authenticated
    resources.pre_cleanup = _observe_cleanup(resources, recorder.blackout_active())
    try:
        if authenticated is not None:
            try:
                if not authenticated.page.is_closed():
                    try:
                        authenticated.context.grant_permissions(
                            ["clipboard-read", "clipboard-write"],
                            origin=UI_ORIGIN,
                        )
                        execute_script(
                            authenticated.page,
                            "() => navigator.clipboard.writeText('')",
                        )
                        resources.clipboard_clean = clipboard_is_empty(authenticated.page)
                    except Error:
                        resources.clipboard_clean = False
            finally:
                close_authenticated(authenticated)
    finally:
        try:
            try:
                recorder.end_blackout()
            finally:
                try:
                    if resources.managed is not None:
                        stop_managed_browser(resources.managed)
                finally:
                    if resources.server is not None:
                        stop_fake_server(resources.server)
        finally:
            observed = _observe_cleanup(resources, recorder.blackout_active())
            receipt = CleanupReceipt.model_validate(
                {
                    "port_2456_available_before": resources.port_available_before,
                    "browser_process_baseline": 0
                    if resources.managed is None
                    else resources.managed.baseline_browser_processes,
                    "playwright_driver_baseline": 0
                    if resources.managed is None
                    else resources.managed.baseline_playwright_drivers,
                    "temporary_directory_baseline": 0
                    if resources.managed is None
                    else resources.managed.baseline_temporary_paths,
                    **observed.model_dump(),
                }
            )
            resources.post_cleanup = receipt
    return receipt
