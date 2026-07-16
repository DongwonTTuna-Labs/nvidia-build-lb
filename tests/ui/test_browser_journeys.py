from importlib.metadata import version
from pathlib import Path

import pytest

from .browser_auth import (
    AuthenticatedSession,
    AuthJourneyContext,
    open_authenticated_session,
)
from .browser_checks import execute_script, focused_id, storage_observation
from .browser_cleanup import (
    BrowserResources,
    assert_native_phase_boundary,
    cleanup_browser_resources,
)
from .browser_credentials import assert_secret_absent, credential_state_observation
from .browser_evidence import (
    BrowserProvenance,
    CleanupChronology,
    EvidenceRecorder,
    ManualQaReceipt,
    ManualScenario,
)
from .browser_observability_contract import (
    authenticated_observability_receipt,
    network_observation_is_allowed,
)
from .browser_owner import run_downstream_journey, run_upstream_journey
from .browser_public import PublicQaResult, capture_public_surfaces
from .browser_receipts import adversarial_receipt
from .browser_runtime import (
    start_fake_server,
    start_managed_browser,
)
from .browser_states import run_clipboard_failure, run_state_recovery
from .browser_zoom_run import run_native_zoom_qa
from .evidence_paths import evidence_directory

pytestmark = pytest.mark.ui_fake

_AXE_ASSET = Path(__file__).parent / "vendor" / "axe-core-4.12.1" / "axe.min.js"
_LOGOUT_UPSTREAM_CREDENTIAL = "synthetic-logout-upstream-custody-value"
_REQUIRED_CAPTURE_NAMES = (
    "admin-login-375",
    "showcase-375",
    "admin-login-768",
    "showcase-768",
    "admin-login-1280",
    "showcase-1280",
    "showcase-reduced-motion",
    "admin-dashboard-post-login-cleanup-375",
    "admin-upstream-form-375",
    "admin-dashboard-post-login-cleanup-768",
    "admin-upstream-form-768",
    "admin-downstream-form-768",
    "admin-destructive-confirmation-768",
    "admin-login-error",
    "admin-initial-503",
    "admin-dashboard-post-login-cleanup",
    "admin-destructive-confirmation-1280",
    "admin-upstream-post-cleanup",
    "admin-downstream-form-1280",
    "admin-downstream-post-cleanup",
    "admin-stale-offline",
    "admin-empty",
    "admin-cjk-xss-safe",
    "admin-clipboard-failure-post-cleanup",
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
)


def _exercise_reload_and_logout(
    session: AuthenticatedSession,
    recorder: EvidenceRecorder,
    admin_bearer: str,
) -> None:
    page = session.page
    session.audit.set_phase("reload_clears_bearer")
    _ = page.reload(wait_until="domcontentloaded")
    page.locator("#admin-bearer").wait_for(state="visible")
    assert focused_id(page) == "admin-bearer"
    assert_secret_absent(page, admin_bearer)
    assert credential_state_observation(page).model_dump() == {
        "admin_bearer_present": False,
        "one_time_token_present": False,
        "copied_credential": False,
    }
    recorder.begin_blackout("admin bearer entered for logout proof")
    session.audit.set_phase("reauthenticate_for_logout")
    page.keyboard.insert_text(admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    assert_secret_absent(page, admin_bearer)
    assert credential_state_observation(page).admin_bearer_present
    recorder.end_blackout()
    session.audit.set_phase("logout")
    page.locator("#add-upstream").focus()
    page.keyboard.press("Enter")
    recorder.begin_blackout("upstream credential entered before logout")
    page.keyboard.insert_text(_LOGOUT_UPSTREAM_CREDENTIAL)
    execute_script(page, "() => document.querySelector('#logout').click()")
    page.locator("#admin-bearer").wait_for(state="visible")
    assert_secret_absent(page, _LOGOUT_UPSTREAM_CREDENTIAL)
    assert not any(credential_state_observation(page).model_dump().values())
    recorder.end_blackout()
    recorder.begin_blackout("admin bearer entered for keyboard logout proof")
    page.keyboard.insert_text(admin_bearer)
    page.keyboard.press("Enter")
    page.locator("#dashboard-title").wait_for(state="visible")
    assert credential_state_observation(page).admin_bearer_present
    recorder.end_blackout()
    page.locator("#logout").focus()
    page.keyboard.press("Enter")
    page.locator("#admin-bearer").wait_for(state="visible")
    assert focused_id(page) == "admin-bearer"
    assert not any(credential_state_observation(page).model_dump().values())
    storage = storage_observation(page)
    assert storage.local == storage.session == storage.cookies == storage.query == 0
    recorder.add_scenario(
        ManualScenario(
            name="reload and logout bearer erasure",
            actions=(
                "Reload authenticated tab and inspect #admin-bearer focus",
                "Enter an upstream credential, dispatch logout, and prove immediate reset",
                "Authenticate by keyboard, focus #logout, and press Enter",
            ),
            observables=(
                "Reload restores unauthenticated shell without a safe API request",
                "Logout clears the open upstream password control before mounting login",
                "Logout aborts pending controller state and focuses #admin-bearer",
                "Browser storage, cookies, URL query, and form controls contain no bearer",
            ),
        )
    )


def _drive_journeys(resources: BrowserResources, recorder: EvidenceRecorder) -> PublicQaResult:
    resources.server = start_fake_server()
    resources.managed = start_managed_browser()
    public = capture_public_surfaces(resources.managed.browser, recorder, _AXE_ASSET)
    public_server_audit = resources.server.state.network_audit()
    assert public.failed_request_events == 0
    assert public.response_events == len(public_server_audit)
    assert public.request_events == public.response_events
    assert all(not item.query_present for item in public_server_audit)
    assert all(network_observation_is_allowed(item) for item in public_server_audit)
    resources.server.state.reset_network_audit()
    resources.authenticated = open_authenticated_session(
        AuthJourneyContext(
            browser=resources.managed.browser,
            recorder=recorder,
            state=resources.server.state,
            axe_asset=_AXE_ASSET,
        )
    )
    resources.axe_network_requests = (
        public.axe_network_requests + resources.authenticated.axe_network_requests
    )
    resources.authenticated.audit.set_phase("upstream_journey")
    run_upstream_journey(resources.authenticated, recorder, resources.server.state)
    resources.authenticated.audit.set_phase("downstream_journey")
    run_downstream_journey(resources.authenticated, recorder, resources.server.state)
    run_state_recovery(resources.authenticated, recorder, resources.server.state)
    run_clipboard_failure(resources.authenticated, recorder, resources.server.state)
    _exercise_reload_and_logout(
        resources.authenticated,
        recorder,
        resources.server.state.admin_bearer,
    )
    assert_secret_absent(
        resources.authenticated.page,
        resources.server.state.admin_bearer,
    )
    return public


def _verify_and_write(
    resources: BrowserResources,
    recorder: EvidenceRecorder,
    public: PublicQaResult,
) -> None:
    assert resources.authenticated is not None
    assert resources.managed is not None
    assert resources.server is not None
    authenticated = resources.authenticated
    assert public.axe_serious + authenticated.axe_serious == 0
    assert public.axe_critical + authenticated.axe_critical == 0
    server_audit = resources.server.state.network_audit()
    observability = authenticated_observability_receipt(authenticated.audit, server_audit)
    assert public.console_errors + observability.application_console_errors == 0
    assert public.page_errors + observability.page_errors == 0
    assert all(not item.query_present for item in server_audit)
    assert all(network_observation_is_allowed(item) for item in server_audit)
    playwright_version = version("playwright")
    assert playwright_version == "1.61.0"
    provenance = BrowserProvenance(
        playwright_version=playwright_version,
        chromium_revision=1228,
        browser_version=resources.managed.version,
        executable_path=str(resources.managed.executable_path),
        actual_process_executables=tuple(
            str(path) for path in resources.managed.process_executables
        ),
    )
    recorder.write(
        "capture-index.json",
        recorder.verified_capture_index(_REQUIRED_CAPTURE_NAMES),
    )
    recorder.write(
        "manual-qa.json",
        ManualQaReceipt(
            provenance=provenance,
            scenarios=recorder.scenarios(),
            axe_serious=0,
            axe_critical=0,
            console_errors=0,
            page_errors=0,
            reduced_motion=public.reduced_motion,
            admin_desktop=authenticated.desktop_layout,
            showcase_desktop=public.desktop_layout,
            observability=observability,
        ),
    )
    recorder.write("adversarial.json", adversarial_receipt())


def test_complete_fake_admin_browser_journeys() -> None:
    # Given: a fresh evidence directory, the exact loopback contract, and managed revision 1228.
    recorder = EvidenceRecorder(evidence_directory())
    resources = BrowserResources()

    try:
        # When: every ordinary public and authenticated scenario owns one context and page.
        public = _drive_journeys(resources, recorder)
    finally:
        _ = cleanup_browser_resources(resources, recorder)
    assert_native_phase_boundary(resources)

    native_server = start_fake_server()
    # When: the sole full-Chromium native phase repeats the owner journey on one page.
    native_receipt = run_native_zoom_qa(recorder, native_server, _AXE_ASSET)

    # Then: both serialized phases share one immutable capture manifest and measured receipts.
    recorder.write("native-zoom.json", native_receipt)
    assert resources.pre_cleanup is not None
    assert resources.post_cleanup is not None
    recorder.write(
        "cleanup.json",
        CleanupChronology(
            chronology=(
                "ordinary_before_cleanup",
                "ordinary_after_cleanup",
                "native_before_cleanup",
                "native_after_cleanup",
            ),
            ordinary_before_cleanup=resources.pre_cleanup,
            ordinary_after_cleanup=resources.post_cleanup,
            native_before_cleanup=native_receipt.cleanup_before,
            native_after_cleanup=native_receipt.cleanup_after,
        ),
    )
    _verify_and_write(resources, recorder, public)
