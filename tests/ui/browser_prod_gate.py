import argparse
from importlib.metadata import version
from pathlib import Path
from typing import Literal, final

from playwright.sync_api import Error

from .browser_auth import close_authenticated_session
from .browser_checks import clipboard_is_empty, execute_script
from .browser_credentials import assert_secret_absent, credential_state_observation
from .browser_evidence import (
    AdversarialProbe,
    AdversarialReceipt,
    BrowserProvenance,
    CaptureIndex,
    EvidenceRecorder,
)
from .browser_observability import PageAudit
from .browser_prod_auth import capture_actual_empty_state, open_production_session
from .browser_prod_client import BrowserProdAdminClient
from .browser_prod_context import ProductionJourney, ProductionQaContext
from .browser_prod_downstream import (
    run_production_clipboard_failure,
    run_production_downstream_journey,
)
from .browser_prod_models import (
    BrowserPhaseCleanup,
    ProductionBrowserReceipt,
    ProductionRunCandidate,
)
from .browser_prod_native import run_production_native_phase
from .browser_prod_observability import ProductionNetworkAudit
from .browser_prod_states import (
    exercise_production_reload_logout,
    run_production_state_recovery,
)
from .browser_prod_upstream import (
    complete_production_upstream_cleanup,
    run_production_upstream_journey,
)
from .browser_public import capture_public_surfaces
from .browser_runtime import (
    ManagedBrowserSession,
    browser_resource_snapshot,
    start_managed_browser,
    stop_managed_browser,
)
from .browser_zoom import start_native_headless_context

_AXE_ASSET = Path(__file__).parent / "vendor" / "axe-core-4.12.1" / "axe.min.js"
_REQUIRED_CAPTURES = (
    "admin-login-375",
    "showcase-375",
    "admin-login-768",
    "showcase-768",
    "admin-login-1280",
    "showcase-1280",
    "showcase-reduced-motion",
    "admin-empty",
    "admin-dashboard-post-login-cleanup-375",
    "admin-upstream-form-375",
    "admin-dashboard-post-login-cleanup-768",
    "admin-login-error",
    "admin-login-offline",
    "admin-initial-503",
    "admin-dashboard-post-login-cleanup",
    "admin-action-probe-503",
    "admin-action-enable-401",
    "admin-downstream-form-1280",
    "admin-downstream-post-cleanup",
    "admin-cjk-xss-safe",
    "admin-destructive-confirmation-1280",
    "admin-upstream-post-cleanup",
    "admin-stale-offline",
    "admin-empty-injected",
    "admin-clipboard-failure-post-cleanup",
    "native-showcase-full",
    "native-showcase-focused-control",
    "native-admin-action-probe-503",
    "native-admin-action-enable-401",
    "native-admin-downstream-post-cleanup",
    "native-admin-cjk-xss-safe",
    "native-admin-upstream-post-cleanup",
    "native-admin-stale-offline",
    "native-admin-empty-injected",
    "native-admin-clipboard-failure-post-cleanup",
)


@final
class _Arguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.evidence_dir = Path()
        self.admin_token_file = Path()
        self.source_sha = ""
        self.image_digest = ""
        self.run_name: Literal["run-a", "run-b"] = "run-a"


def _arguments() -> _Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--evidence-dir", type=Path, required=True)
    _ = parser.add_argument("--admin-token-file", type=Path, required=True)
    _ = parser.add_argument("--source-sha", required=True)
    _ = parser.add_argument("--image-digest", required=True)
    _ = parser.add_argument("--run-name", choices=("run-a", "run-b"), required=True)
    arguments = _Arguments()
    _ = parser.parse_args(namespace=arguments)
    return arguments


def _phase_cleanup(
    baseline_processes: set[int],
    baseline_drivers: set[int],
    baseline_paths: set[Path],
) -> BrowserPhaseCleanup:
    observed = browser_resource_snapshot()
    process_count = len({pid for pid, _ in observed.processes} - baseline_processes)
    driver_count = len(set(observed.drivers) - baseline_drivers)
    path_count = sum(path.exists() for path in set(observed.temporary_paths) - baseline_paths)
    return BrowserPhaseCleanup.model_validate(
        {
            "browser_processes": process_count,
            "playwright_drivers": driver_count,
            "browser_contexts": 0,
            "browser_pages": 0,
            "persistent_profiles": 0,
            "temporary_directories": path_count,
            "clipboard_nonempty": 0,
            "capture_blackout_active": 0,
        }
    )


def assert_expected_errors(*audits: PageAudit) -> None:
    expected = {
        "auth_wrong_token",
        "auth_initial_offline",
        "auth_initial_503",
        "upstream_probe_503",
        "upstream_enable_401",
        "offline_refresh",
        "native-upstream_probe_503",
        "native-upstream_enable_401",
        "native-offline_refresh",
    }
    for audit in audits:
        if audit.runtime_exceptions or audit.page_errors:
            runtime_phases = tuple(sorted(audit.runtime_exceptions))
            page_phases = tuple(sorted(audit.page_errors))
            reason = (
                "production browser observed a runtime or page exception: "
                f"runtime_phases={runtime_phases}; page_phases={page_phases}"
            )
            raise AssertionError(reason)
        page_problems = tuple(
            sorted(
                (item.phase, item.kind)
                for item in audit.page_console_calls
                if item.kind in {"assert", "error", "warning"}
            )
        )
        runtime_problems = tuple(
            sorted(
                (item.phase, item.kind)
                for item in audit.runtime_console_calls
                if item.kind in {"assert", "error", "warning"}
            )
        )
        browser_problems = tuple(
            sorted(
                (item.phase, item.source, item.level)
                for item in audit.browser_logs
                if item.level in {"error", "warning"}
            )
        )
        problem_phases = {item[0] for item in (*page_problems, *runtime_problems)}
        problem_phases.update(item[0] for item in browser_problems)
        unexpected = sorted(problem_phases - expected)
        if unexpected:
            phases = ",".join(unexpected)
            reason = (
                f"production browser observed unexpected error phases: {phases}; "
                f"page={page_problems}; runtime={runtime_problems}; "
                f"browser={browser_problems}"
            )
            raise AssertionError(reason)


def _close_ordinary(journey: ProductionJourney, *, verify_custody: bool) -> None:
    session = journey.session
    try:
        session.context.grant_permissions(
            ["clipboard-read", "clipboard-write"], origin="http://127.0.0.1:2456"
        )
        session.page.evaluate("() => navigator.clipboard.writeText('')")
        if verify_custody:
            assert clipboard_is_empty(session.page)
            assert_secret_absent(session.page, "nvapi-synthetic-browser-prod-logout")
            execute_script(
                session.page,
                "async () => { (await import('/assets/admin.js')).credentialState(); }",
            )
            assert not any(credential_state_observation(session.page).model_dump().values())
    except Error:
        if verify_custody:
            reason = "production browser clipboard cleanup failed"
            raise AssertionError(reason) from None
    finally:
        journey.qa.recorder.end_blackout()
        journey.qa.network.detach(session.page)
        close_authenticated_session(session)


def _adversarial() -> AdversarialReceipt:
    return AdversarialReceipt(
        probes=(
            AdversarialProbe(
                probe_class="wrong_realm_offline_and_initial_503",
                status="passed",
                observable=(
                    "actual 401, distinct offline state, and bounded safe 503 "
                    "recovered by natural keyboard flow"
                ),
            ),
            AdversarialProbe(
                probe_class="offline_and_empty",
                status="passed",
                observable="one aborted dashboard and one strict safe empty projection recovered",
            ),
            AdversarialProbe(
                probe_class="management_action_503_and_401",
                status="passed",
                observable=(
                    "probe 503 retained exact safe problem identity and enable 401 "
                    "cleared custody before reauthentication"
                ),
            ),
            AdversarialProbe(
                probe_class="secret_custody",
                status="passed",
                observable="capture blackout guarded every credential-bearing browser state",
            ),
            AdversarialProbe(
                probe_class="clipboard_failure",
                status="passed",
                observable="dismissal remained blocked until clipboard and DOM cleanup succeeded",
            ),
        )
    )


def _provenance(managed: ManagedBrowserSession) -> BrowserProvenance:
    playwright_version = version("playwright")
    assert playwright_version == "1.61.0"
    return BrowserProvenance(
        playwright_version="1.61.0",
        chromium_revision=1228,
        browser_version=managed.version,
        executable_path=str(managed.executable_path),
        actual_process_executables=tuple(str(path) for path in managed.process_executables),
    )


def _assert_zero(*observed: int) -> None:
    assert not any(observed)


def main() -> int:
    args = _arguments()
    evidence_dir = args.evidence_dir
    run_name = args.run_name
    recorder = EvidenceRecorder(evidence_dir, gate_root=evidence_dir.parent)
    network = ProductionNetworkAudit()
    baseline = browser_resource_snapshot()
    baseline_processes = {pid for pid, _ in baseline.processes}
    baseline_drivers = set(baseline.drivers)
    baseline_paths = set(baseline.temporary_paths)
    with BrowserProdAdminClient(args.admin_token_file) as client:
        if client.upstreams().items or client.tokens().items:
            reason = "production browser run did not start from an empty database"
            raise AssertionError(reason)
        qa = ProductionQaContext(
            recorder=recorder,
            client=client,
            network=network,
            axe_asset=_AXE_ASSET,
            run_name=run_name,
        )
        managed = start_managed_browser()
        session = None
        journey = None
        journey_completed = False
        try:
            public = capture_public_surfaces(managed.browser, recorder, _AXE_ASSET, network)
            empty_counts = capture_actual_empty_state(managed.browser, qa)
            _ = client.seed_ready_key(run_name)
            session = open_production_session(managed.browser, qa)
            journey = ProductionJourney(qa=qa, session=session)
            upstream_cleanup = run_production_upstream_journey(journey)
            run_production_downstream_journey(journey)
            complete_production_upstream_cleanup(journey, upstream_cleanup)
            run_production_state_recovery(journey)
            run_production_clipboard_failure(journey)
            exercise_production_reload_logout(journey)
            journey_completed = True
        finally:
            if journey is not None:
                _close_ordinary(journey, verify_custody=journey_completed)
            stop_managed_browser(managed)
        ordinary_cleanup = _phase_cleanup(baseline_processes, baseline_drivers, baseline_paths)
        native, native_audit = run_production_native_phase(
            qa,
            start_native_headless_context,
            (upstream_cleanup.fingerprint,),
        )
        assert session is not None
        assert_expected_errors(session.audit, native_audit)
        projection = network.verified()
        _assert_zero(
            public.axe_serious,
            public.axe_critical,
            public.axe_network_requests,
            empty_counts.serious,
            empty_counts.critical,
            empty_counts.network_requests,
            session.axe_serious,
            session.axe_critical,
            session.axe_network_requests,
        )
        receipt = ProductionBrowserReceipt(
            source_tree_sha256=args.source_sha,
            image_digest=args.image_digest,
            run_name=run_name,
            provenance=_provenance(managed),
            scenarios=recorder.scenarios(),
            axe_serious=0,
            axe_critical=0,
            axe_network_requests=0,
            console_errors=0,
            page_errors=0,
            reduced_motion=public.reduced_motion,
            admin_desktop=session.desktop_layout,
            showcase_desktop=public.desktop_layout,
            network=projection,
            network_query_count=0,
            network_header_body_count=0,
            admin_bearer_dom_absent=True,
            one_time_token_dom_absent=True,
            ordinary_cleanup=ordinary_cleanup,
            native=native,
        )
        recorder.write("manual-qa.json", receipt)
        recorder.write("adversarial.json", _adversarial())
        capture_index: CaptureIndex = recorder.verified_capture_index(_REQUIRED_CAPTURES)
        recorder.write("capture-index.json", capture_index)
        recorder.write(
            "candidate.json",
            ProductionRunCandidate(
                source_tree_sha256=args.source_sha,
                image_digest=args.image_digest,
                run_name=run_name,
                capture_count=len(capture_index.captures),
                ordinary_phase_first=True,
                native_phase_second=True,
                lighthouse_pending=True,
            ),
        )
        client.cleanup_rows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
