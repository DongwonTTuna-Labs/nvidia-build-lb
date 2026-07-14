from hashlib import sha256
from json import dumps
from pathlib import Path

import pytest

from .browser_checks import ZoomObservation
from .browser_determinism import observe_run_receipts, write_run_comparison
from .browser_evidence import (
    CaptureIndex,
    CaptureRecord,
    CleanupChronology,
    CleanupReceipt,
    ResourceObservation,
)
from .browser_native_receipt import NativeDeterministicProjection, NativeZoomReceipt

pytestmark = pytest.mark.ui_fake


def _write_json(path: Path, value: CaptureIndex | CleanupChronology | NativeZoomReceipt) -> None:
    _ = path.write_text(value.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _write_run(root: Path, leaf: str, names: tuple[str, ...]) -> Path:
    run = root / "runs" / leaf
    captures = run / "captures"
    captures.mkdir(parents=True)
    records: list[CaptureRecord] = []
    for name in names:
        content = name.encode()
        path = captures / f"{name}.png"
        _ = path.write_bytes(content)
        records.append(
            CaptureRecord(
                name=name,
                route="http://127.0.0.1:2456/admin",
                state="synthetic",
                viewport="640x450",
                reduced_motion=False,
                native_zoom=name.startswith("native-"),
                path=str(path),
                sha256=sha256(content).hexdigest(),
                byte_count=len(content),
                source_newest_mtime_ns=1,
                capture_mtime_ns=2,
                source_paths=(),
                content_row_coverage=1,
            )
        )
    _write_json(run / "capture-index.json", CaptureIndex(captures=tuple(records)))
    _write_json(
        run / "cleanup.json",
        CleanupChronology(
            chronology=(
                "ordinary_before_cleanup",
                "ordinary_after_cleanup",
                "native_before_cleanup",
                "native_after_cleanup",
            ),
            ordinary_before_cleanup=ResourceObservation(
                fake_server_threads=1,
                port_2456_listeners=1,
                browser_processes=1,
                playwright_drivers=1,
                browser_contexts=1,
                browser_pages=1,
                persistent_profiles=0,
                temporary_directories=1,
                clipboard_nonempty=0,
                capture_blackout_active=0,
                axe_network_requests=0,
            ),
            ordinary_after_cleanup=CleanupReceipt(
                port_2456_available_before=True,
                browser_process_baseline=2,
                playwright_driver_baseline=1,
                temporary_directory_baseline=3,
                fake_server_threads=0,
                port_2456_listeners=0,
                browser_processes=0,
                playwright_drivers=0,
                browser_contexts=0,
                browser_pages=0,
                persistent_profiles=0,
                temporary_directories=0,
                clipboard_nonempty=0,
                capture_blackout_active=0,
                axe_network_requests=0,
            ),
            native_before_cleanup=ResourceObservation(
                fake_server_threads=1,
                port_2456_listeners=1,
                browser_processes=1,
                playwright_drivers=1,
                browser_contexts=1,
                browser_pages=1,
                persistent_profiles=1,
                temporary_directories=2,
                clipboard_nonempty=0,
                capture_blackout_active=0,
                axe_network_requests=0,
            ),
            native_after_cleanup=CleanupReceipt(
                port_2456_available_before=True,
                browser_process_baseline=2,
                playwright_driver_baseline=1,
                temporary_directory_baseline=3,
                fake_server_threads=0,
                port_2456_listeners=0,
                browser_processes=0,
                playwright_drivers=0,
                browser_contexts=0,
                browser_pages=0,
                persistent_profiles=0,
                temporary_directories=0,
                clipboard_nonempty=0,
                capture_blackout_active=0,
                axe_network_requests=0,
            ),
        ),
    )
    deterministic = NativeDeterministicProjection(
        preference_sha256="a" * 64,
        physical_width=1280,
        physical_height=900,
        css_viewport_width=632.5,
        css_viewport_height=450,
        scroll_width=632,
        observation=ZoomObservation(
            layout_zoom=2,
            inner_width=640,
            device_pixel_ratio=2,
            visual_viewport_scale=1,
            narrow_media=True,
            dom_hash="b" * 64,
        ),
        dom_hash_after="b" * 64,
        stable_dom_hashes=("c" * 64,),
        stable_focus_stops=(18,),
        stable_focus_clipped=(0,),
        stable_focus_hidden=(0,),
        stable_focus_covered=(0,),
        capture_ids=tuple(name for name in names if name.startswith("native-")),
    )
    _write_json(
        run / "native-zoom.json",
        NativeZoomReceipt(
            deterministic=deterministic,
            executable_path="/managed/chromium-1228/chrome",
            chromium_revision=1228,
            contexts_started=1,
            pages_started=1,
            maximum_live_contexts=1,
            maximum_live_pages=1,
            shared_browser_used=False,
            browser_process_baseline=2,
            playwright_driver_baseline=1,
            profile_path_present_before_cleanup=True,
            profile_paths_after_cleanup=0,
            browser_processes_after_cleanup=0,
            playwright_drivers_after_cleanup=0,
            cleanup_before=ResourceObservation(
                fake_server_threads=1,
                port_2456_listeners=1,
                browser_processes=1,
                playwright_drivers=1,
                browser_contexts=1,
                browser_pages=1,
                persistent_profiles=1,
                temporary_directories=2,
                clipboard_nonempty=0,
                capture_blackout_active=0,
                axe_network_requests=0,
            ),
            cleanup_after=CleanupReceipt(
                port_2456_available_before=True,
                browser_process_baseline=2,
                playwright_driver_baseline=1,
                temporary_directory_baseline=3,
                fake_server_threads=0,
                port_2456_listeners=0,
                browser_processes=0,
                playwright_drivers=0,
                browser_contexts=0,
                browser_pages=0,
                persistent_profiles=0,
                temporary_directories=0,
                clipboard_nonempty=0,
                capture_blackout_active=0,
                axe_network_requests=0,
            ),
        ),
    )
    command = {
        "command": ["uv", "run", "pytest", "-m", "ui_fake", "-q"],
        "process_id": 123,
        "started_at_ns": 1,
        "finished_at_ns": 2,
        "exit_code": 0,
        "selected_node_count": 1,
        "passed_node_count": 1,
        "failed_node_count": 0,
        "skipped_node_count": 0,
        "node_manifest": [{"node_id": "tests/ui/test_one.py::test_one", "outcome": "passed"}],
    }
    _ = (run / "command-receipt.json").write_text(dumps(command, indent=2) + "\n", encoding="utf-8")
    return run


def test_observer_writes_top_level_json_and_markdown_for_matching_runs(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task-4-nvidia-build-lb"
    names = ("ordinary-login", "native-showcase")
    first = _write_run(root, "fresh-a", names)
    second = _write_run(root, "fresh-b", names)

    receipt = write_run_comparison(first, second)

    assert receipt.passed is True
    assert receipt.capture_order_count_match is True
    assert receipt.native_deterministic_projection_match is True
    assert receipt.ordinary_cleanup_zero is True
    assert receipt.native_cleanup_zero is True
    assert receipt.command_exit_zero is True
    assert receipt.node_manifest_match is True
    assert (root / "determinism.json").is_file()
    markdown = (root / "determinism.md").read_text(encoding="utf-8")
    assert "fresh-a" in markdown
    assert "fresh-b" in markdown
    assert "PASS" in markdown


def test_observer_rejects_reordered_captures_and_native_projection_drift(
    tmp_path: Path,
) -> None:
    root = tmp_path / "task-4-nvidia-build-lb"
    first = _write_run(root, "fresh-a", ("ordinary-login", "native-showcase"))
    second = _write_run(root, "fresh-b", ("native-showcase", "ordinary-login"))
    native_path = second / "native-zoom.json"
    native = NativeZoomReceipt.model_validate_json(native_path.read_text(encoding="utf-8"))
    changed = native.model_copy(
        update={
            "deterministic": native.deterministic.model_copy(update={"stable_focus_stops": (19,)})
        }
    )
    _write_json(native_path, changed)

    receipt = observe_run_receipts(first, second)

    assert receipt.capture_order_count_match is False
    assert receipt.native_deterministic_projection_match is False
    assert receipt.passed is False


def test_observer_rehashes_capture_files_after_each_run_receipt(tmp_path: Path) -> None:
    root = tmp_path / "task-4-nvidia-build-lb"
    names = ("ordinary-login", "native-showcase")
    first = _write_run(root, "fresh-a", names)
    second = _write_run(root, "fresh-b", names)
    _ = (second / "captures" / "ordinary-login.png").write_bytes(b"tampered")

    receipt = observe_run_receipts(first, second)

    assert receipt.first.capture_files_current is True
    assert receipt.second.capture_files_current is False
    assert receipt.passed is False
