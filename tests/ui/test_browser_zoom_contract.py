from pathlib import Path

import pytest

from .browser_checks import ZoomObservation
from .browser_runtime import MANAGED_BROWSERS, BrowserRuntimeError
from .browser_zoom import NativeZoomResult, assert_native_zoom_contract

pytestmark = pytest.mark.ui_fake


def _result(profile_path: Path, **changes: float | bool) -> NativeZoomResult:
    observation = ZoomObservation(
        layout_zoom=float(changes.get("layout_zoom", 2)),
        inner_width=int(changes.get("inner_width", 640)),
        device_pixel_ratio=float(changes.get("device_pixel_ratio", 2)),
        visual_viewport_scale=float(changes.get("visual_viewport_scale", 1)),
        narrow_media=bool(changes.get("narrow_media", True)),
        dom_hash="a" * 64,
    )
    return NativeZoomResult(
        profile_path=profile_path,
        executable_path=MANAGED_BROWSERS / "chromium-1228" / "chrome-linux64" / "chrome",
        chromium_revision=1228,
        launch_mode="full_chromium_new_headless",
        preference_path="partition.per_host_zoom_levels.x.127.0.0.1.zoom_level",
        preference_level=3.8017840169239308,
        preference_sha256="0e32b1d46fe3d860c88002c2da225668b32d728ed12bbb65bd6e52e13fd827c1",
        physical_width=int(changes.get("physical_width", 1280)),
        physical_height=int(changes.get("physical_height", 900)),
        css_viewport_width=632.5,
        css_viewport_height=441.5,
        scroll_width=int(changes.get("scroll_width", 632)),
        observation=observation,
        dom_hash_after="a" * 64,
    )


def test_native_zoom_acceptance_is_checked_only_after_the_ordinary_phase_gate(
    tmp_path: Path,
) -> None:
    # Given: the exact measured native metric tuple produced by the combined journey.
    result = _result(tmp_path / "nblb-native-contract-fixture")

    # When/Then: the strict acceptance projection passes without launching a native-first test.
    assert_native_zoom_contract(result)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("physical_width", 1279),
        ("layout_zoom", 1),
        ("inner_width", 641),
        ("visual_viewport_scale", 2),
        ("narrow_media", False),
        ("scroll_width", 641),
    ],
)
def test_native_zoom_rejects_each_non_native_or_overflow_metric(
    tmp_path: Path,
    field: str,
    value: int | bool,
) -> None:
    # Given: one observed native acceptance metric drifts from the locked tuple.
    result = _result(tmp_path / "nblb-native-contract-fixture", **{field: value})

    # When/Then: the same acceptance boundary fails closed.
    with pytest.raises(BrowserRuntimeError):
        assert_native_zoom_contract(result)
