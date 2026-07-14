from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from playwright.sync_api import Error
from pydantic import ValidationError

from . import browser_cleanup
from .browser_auth import AuthenticatedSession
from .browser_checks import AdminDesktopObservation
from .browser_evidence import EvidenceRecorder
from .browser_observability import PageAudit
from .browser_receipts import adversarial_receipt
from .browser_runtime import (
    MANAGED_BROWSERS,
    BrowserResourceSnapshot,
    ManagedBrowserSession,
    authority_is_free,
    browser_resource_snapshot,
    new_managed_processes,
    start_fake_server,
    start_managed_browser,
    stop_fake_server,
    stop_managed_browser,
)

pytestmark = pytest.mark.ui_fake

_DESKTOP = AdminDesktopObservation(
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


def _evidence_leaf(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / "task-4-nvidia-build-lb" / "runs" / name
    directory.parent.mkdir(parents=True)
    return directory


def test_cleanup_continues_after_an_authenticated_close_failure(tmp_path: Path) -> None:
    # Given: every task resource is live and authenticated close will fail first.
    resources = browser_cleanup.BrowserResources()
    resources.server = start_fake_server()
    resources.managed = start_managed_browser()
    context = resources.managed.browser.new_context()
    page = context.new_page()
    _ = page.goto("http://127.0.0.1:2456/admin", wait_until="domcontentloaded")
    resources.authenticated = AuthenticatedSession(
        context=context,
        page=page,
        audit=PageAudit(),
        axe_serious=0,
        axe_critical=0,
        axe_network_requests=0,
        desktop_layout=_DESKTOP,
    )
    directory = _evidence_leaf(tmp_path, "cleanup-interruption")
    recorder = EvidenceRecorder(directory, directory.parent)

    def fail_authenticated_close(session: AuthenticatedSession) -> None:
        del session
        reason = "synthetic close failure"
        raise Error(reason)

    try:
        # When: cleanup encounters the first close failure.
        with pytest.raises(Error, match="synthetic close failure"):
            _ = browser_cleanup.cleanup_browser_resources(
                resources,
                recorder,
                fail_authenticated_close,
            )

        # Then: later browser, server, and port cleanup still completes.
        assert resources.managed.browser.is_connected() is False
        assert resources.server.thread.is_alive() is False
        assert authority_is_free()
    finally:
        if not page.is_closed():
            page.close()
        if resources.managed.browser.is_connected():
            stop_managed_browser(resources.managed)
        if resources.server.thread.is_alive():
            stop_fake_server(resources.server)


def test_cleanup_receipt_rejects_an_observed_process_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Given: one managed browser and an observer that reports its process remains live.
    resources = browser_cleanup.BrowserResources()
    resources.managed = start_managed_browser()
    directory = _evidence_leaf(tmp_path, "cleanup-process-leak")
    recorder = EvidenceRecorder(directory, directory.parent)

    def observed_process_leak(runtime: ManagedBrowserSession) -> int:
        del runtime
        return 1

    monkeypatch.setattr(browser_cleanup, "managed_process_count", observed_process_leak)

    # When/Then: measured nonzero state cannot be serialized as a clean receipt.
    with pytest.raises(ValidationError, match="browser_processes"):
        _ = browser_cleanup.cleanup_browser_resources(resources, recorder)
    assert resources.managed.browser.is_connected() is False


def test_resource_snapshot_diff_excludes_a_preexisting_managed_process() -> None:
    # Given: an approved process exists before a second approved process starts.
    executable = MANAGED_BROWSERS / "chromium-1228" / "chrome-linux64" / "chrome"
    baseline = BrowserResourceSnapshot(processes=((101, executable),), temporary_paths=())
    observed = BrowserResourceSnapshot(
        processes=((101, executable), (202, executable)),
        temporary_paths=(),
    )

    # When: the task-owned process delta is projected.
    task_processes = new_managed_processes(baseline, observed)

    # Then: the baseline process is excluded and only the new process remains.
    assert task_processes == ((202, executable),)


def test_resource_snapshot_tracks_the_browser_fontconfig_directory() -> None:
    with TemporaryDirectory(prefix="nblb-fontconfig-") as temporary:
        snapshot = browser_resource_snapshot()

        assert Path(temporary) in snapshot.temporary_paths


def test_cleanup_receipt_observes_capture_blackout_after_release(tmp_path: Path) -> None:
    resources = browser_cleanup.BrowserResources()
    directory = _evidence_leaf(tmp_path, "cleanup-blackout")
    recorder = EvidenceRecorder(directory, directory.parent)
    recorder.begin_blackout("synthetic credential custody")

    receipt = browser_cleanup.cleanup_browser_resources(resources, recorder)

    assert resources.pre_cleanup is not None
    assert resources.pre_cleanup.capture_blackout_active == 1
    assert receipt.capture_blackout_active == 0
    assert recorder.blackout_active() is False


def test_adversarial_receipt_does_not_self_attest_cleanup_or_determinism() -> None:
    # Given: the per-run adversarial receipt is created before cleanup and cross-run comparison.
    receipt = adversarial_receipt()

    # When: its claimed probe classes are enumerated.
    probe_classes = {probe.probe_class for probe in receipt.probes}

    # Then: later observers exclusively own cleanup, interruption, and determinism claims.
    assert probe_classes.isdisjoint({"hung_long_commands", "flaky_tests", "repeated_interruptions"})
