from hashlib import sha256
from inspect import getsource
from pathlib import Path
from typing import final

import pytest
from playwright.sync_api import Error

from tests import conftest as receipt_conftest

from . import (
    browser_auth,
    browser_determinism,
    browser_evidence,
    browser_png,
    browser_public,
    browser_runtime,
    browser_zoom,
    lighthouse_gate,
    test_browser_journeys,
)
from .browser_evidence import CaptureRecord, EvidenceRecorder
from .browser_observability import PageAudit
from .browser_runtime import MANAGED_BROWSERS, BrowserRuntimeError

pytestmark = pytest.mark.ui_fake


@final
class _FailingPage:
    def close(self) -> None:
        reason = "synthetic page close failure"
        raise Error(reason)


@final
class _ContextDouble:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@final
class _ChromiumDouble:
    def __init__(self, executable_path: Path) -> None:
        self.executable_path = str(executable_path)
        self.launch_calls = 0


@final
class _PlaywrightDouble:
    def __init__(self, executable_path: Path) -> None:
        self.chromium = _ChromiumDouble(executable_path)


@final
class _NativeContextDouble:
    def __init__(self, page_count: int) -> None:
        self.pages = tuple(_FailingPage() for _ in range(page_count))


def _capture_record(directory: Path, name: str, content: bytes) -> CaptureRecord:
    path = directory / "captures" / f"{name}.png"
    _ = path.write_bytes(content)
    return CaptureRecord(
        name=name,
        route="http://127.0.0.1:2456/showcase",
        state="synthetic",
        viewport="1280x900",
        reduced_motion=False,
        native_zoom=False,
        path=str(path),
        sha256=sha256(content).hexdigest(),
        byte_count=len(content),
        source_newest_mtime_ns=1,
        capture_mtime_ns=2,
        source_paths=(),
    )


def test_public_page_close_failure_still_closes_the_scenario_context() -> None:
    # Given: the page close fails after its audit detached.
    context = _ContextDouble()

    # When/Then: context cleanup still runs and the original close failure propagates.
    with pytest.raises(Error, match="synthetic page close failure"):
        browser_public.close_public_page(PageAudit(), _FailingPage(), context)
    assert context.closed is True


def test_authenticated_page_close_failure_still_closes_the_scenario_context() -> None:
    # Given: an authenticated page close fails.
    context = _ContextDouble()

    # When/Then: the context is independently closed by nested-finally cleanup.
    with pytest.raises(Error, match="synthetic page close failure"):
        browser_auth.close_page_context(_FailingPage(), context)
    assert context.closed is True


def test_authenticated_viewports_do_not_resize_a_shared_page() -> None:
    # Given: every viewport is required to own a fresh context and page.
    source = getsource(browser_auth)

    # When/Then: the authenticated browser path has no post-launch viewport resize.
    assert "set_viewport_size" not in source


def test_managed_browser_cache_is_portable_but_absolute_and_revision_pinned(
    tmp_path: Path,
) -> None:
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    cache_alias = tmp_path / "cache-alias"
    cache_alias.symlink_to(real_cache, target_is_directory=True)
    default = browser_runtime.resolve_managed_browsers(None, tmp_path)
    configured = browser_runtime.resolve_managed_browsers(
        str(tmp_path / "managed"), Path("/ignored")
    )
    configured_alias = browser_runtime.resolve_managed_browsers(str(cache_alias), Path("/ignored"))

    assert default == tmp_path / ".cache" / "ms-playwright"
    assert configured == tmp_path / "managed"
    assert configured_alias == real_cache
    with pytest.raises(ValueError, match="must be absolute"):
        _ = browser_runtime.resolve_managed_browsers("relative/cache", tmp_path)
    assert "/home/dongwonttuna" not in getsource(browser_runtime)
    assert lighthouse_gate.CHROME == (
        MANAGED_BROWSERS / "chromium-1228" / "chrome-linux64" / "chrome"
    )


def test_browser_provenance_canonicalizes_a_symlinked_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_cache = tmp_path / "real-cache"
    executable = real_cache / "chromium-1228" / "chrome-linux64" / "chrome"
    executable.parent.mkdir(parents=True)
    executable.touch()
    headless = real_cache / "chromium_headless_shell-1228" / "chrome-linux" / "headless_shell"
    headless.parent.mkdir(parents=True)
    headless.touch()
    cache_alias = tmp_path / "cache-alias"
    cache_alias.symlink_to(real_cache, target_is_directory=True)

    process_root = tmp_path / "proc"
    process = process_root / "123"
    process.mkdir(parents=True)
    (process / "exe").symlink_to(executable)
    _ = (process / "cmdline").write_bytes(b"")
    temporary_root = tmp_path / "tmp"
    temporary_root.mkdir()
    monkeypatch.setattr(browser_runtime, "MANAGED_BROWSERS", cache_alias)
    monkeypatch.setattr(browser_runtime, "_PROCESS_ROOT", process_root)
    monkeypatch.setattr(browser_runtime, "_TEMP_ROOT", temporary_root)
    monkeypatch.setattr(browser_zoom, "MANAGED_BROWSERS", cache_alias)

    snapshot = browser_runtime.browser_resource_snapshot()
    native_executable = browser_zoom.validated_native_executable(
        _PlaywrightDouble(cache_alias / executable.relative_to(real_cache))
    )

    assert snapshot.processes == ((123, executable.resolve()),)
    assert native_executable == executable.resolve()


def test_native_launch_rejects_the_headless_shell_before_starting() -> None:
    # Given: Playwright resolves its native executable to the managed headless shell.
    executable = (
        MANAGED_BROWSERS / "chromium_headless_shell-1228" / "chrome-linux" / "headless_shell"
    )
    playwright = _PlaywrightDouble(executable)

    # When/Then: native provenance fails before persistent-context launch.
    with pytest.raises(BrowserRuntimeError, match="full managed Chromium"):
        _ = browser_zoom.validated_native_executable(playwright)
    source = getsource(browser_zoom.start_native_headless_context)
    assert source.index("validated_native_executable") < source.index("launch_persistent_context")
    assert playwright.chromium.launch_calls == 0


@pytest.mark.parametrize("page_count", [0, 2])
def test_native_capture_requires_exactly_one_launch_created_page(page_count: int) -> None:
    # Given: persistent launch exposes zero or multiple pages.
    context = _NativeContextDouble(page_count)

    # When/Then: the native phase rejects it before using any page.
    with pytest.raises(BrowserRuntimeError, match="exactly one launch-created page"):
        _ = browser_zoom.require_launch_created_page(context)


def test_capture_manifest_rejects_reversed_order(tmp_path: Path) -> None:
    # Given: capture bytes are valid but their observed order is reversed.
    directory = tmp_path / "task-4-nvidia-build-lb" / "runs" / "ordered-manifest"
    directory.parent.mkdir(parents=True)
    recorder = EvidenceRecorder(directory, directory.parent)
    second = _capture_record(directory, "second", b"second")
    first = _capture_record(directory, "first", b"first")
    recorder.add_capture_records((second, first))

    # When/Then: an expected first-then-second manifest cannot be reduced to a set.
    with pytest.raises(browser_evidence.CaptureVerificationError, match="order"):
        _ = recorder.verified_capture_index(("first", "second"))


def test_browser_gate_has_an_observer_owned_determinism_comparator() -> None:
    # Given: two fresh runs must be compared outside either run receipt.
    comparator = getattr(browser_determinism, "compare_run_receipts", None)

    # When/Then: a pure observer seam exists for the locked deterministic projection.
    assert callable(comparator)


def test_command_receipt_uses_root_scope_and_final_marker_selection() -> None:
    # Given: the receipt plugin is registered at the test-suite root.
    source = getsource(receipt_conftest)

    # When/Then: configure owns start time and final marker selection is observed last.
    assert "def pytest_configure(" in source
    assert "def pytest_sessionstart(" not in source
    assert "@pytest.hookimpl(trylast=True)" in source


def test_complete_journey_owns_ordinary_zero_gate_then_native() -> None:
    # Given: the canonical browser journey is the target-level two-phase orchestrator.
    source = getsource(test_browser_journeys.test_complete_fake_admin_browser_journeys)

    # When/Then: phase order and the zero-boundary assertion are explicit in current code.
    assert "run_native_zoom_qa" in source
    assert "assert_native_phase_boundary" in source


def test_native_capture_uses_measured_physical_document_clip() -> None:
    source = getsource(browser_evidence.EvidenceRecorder.capture)

    assert "capture_native_document_png" in source
    assert "if spec.native_zoom" in source


def test_native_capture_stitches_nonexpanding_viewport_surfaces_without_emulation() -> None:
    source = getsource(browser_png)

    assert "Page.captureScreenshot" in source
    assert "Page.getLayoutMetrics" in source
    assert '"captureBeyondViewport": False' in source
    assert "stitch_viewport_tiles" in source
    assert '"clip"' not in source
    assert '"optimizeForSpeed": True' in source
    assert "Emulation." not in source


def test_capture_records_measured_pixel_geometry_and_landmarks() -> None:
    fields = browser_evidence.CaptureRecord.model_fields

    assert {"pixel_width", "pixel_height", "landmarks"} <= fields.keys()
