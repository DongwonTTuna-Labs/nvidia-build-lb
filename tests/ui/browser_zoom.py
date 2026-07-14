from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import ClassVar, Final, Literal, Protocol

from playwright.sync_api import BrowserContext, Page, Playwright
from pydantic import BaseModel, ConfigDict, Field

from .browser_checks import ZoomObservation, evaluate_string
from .browser_runtime import (
    MANAGED_BROWSERS,
    UI_ORIGIN,
    BrowserRuntimeError,
)

_PHYSICAL_WIDTH: Final = 1280
_PHYSICAL_HEIGHT: Final = 900
_FULL_CHROMIUM_NATIVE_HEADLESS_ARGS: Final = ("--headless=new",)
_PREFERENCE_PATH: Final = "partition.per_host_zoom_levels.x.127.0.0.1.zoom_level"
_PREFERENCE_LEVEL: Final = 3.8017840169239308
NATIVE_PREFERENCES: Final = (
    '{"partition":{"per_host_zoom_levels":{"x":{"127.0.0.1":{"zoom_level":3.8017840169239308}}}}}'
)
_PAGE_STATE_SCRIPT: Final = """() => JSON.stringify({
inner_width: window.innerWidth,
outer_width: window.outerWidth,
outer_height: window.outerHeight,
device_pixel_ratio: window.devicePixelRatio,
visual_viewport_scale: window.visualViewport.scale,
narrow_media: window.matchMedia("(max-width: 767px)").matches,
scroll_width: document.documentElement.scrollWidth
})"""


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class _PageState(_StrictModel):
    inner_width: int
    outer_width: int
    outer_height: int
    device_pixel_ratio: float
    visual_viewport_scale: float
    narrow_media: bool
    scroll_width: int


class _CssVisualViewport(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    client_width: float = Field(alias="clientWidth")
    client_height: float = Field(alias="clientHeight")
    zoom: float


class _LayoutMetrics(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    css_visual_viewport: _CssVisualViewport = Field(alias="cssVisualViewport")


type _JsonValue = str | int | float | bool | None | list[_JsonValue] | dict[str, _JsonValue]


class _LayoutMetricSession(Protocol):
    def send(self, method: str) -> dict[str, _JsonValue]: ...


class _ChromiumPath(Protocol):
    @property
    def executable_path(self) -> str: ...


class _PlaywrightPath(Protocol):
    @property
    def chromium(self) -> _ChromiumPath: ...


class _PagesOwner[T](Protocol):
    @property
    def pages(self) -> Sequence[T]: ...


@dataclass(frozen=True, slots=True)
class NativeZoomResult:
    profile_path: Path
    executable_path: Path
    chromium_revision: Literal[1228]
    launch_mode: Literal["full_chromium_new_headless"]
    preference_path: str
    preference_level: float
    preference_sha256: str
    physical_width: int
    physical_height: int
    css_viewport_width: float
    css_viewport_height: float
    scroll_width: int
    observation: ZoomObservation
    dom_hash_after: str


def _dom_hash(page: Page) -> str:
    return sha256(page.content().encode()).hexdigest()


def _read_layout_metrics(session: _LayoutMetricSession) -> _LayoutMetrics:
    return _LayoutMetrics.model_validate(session.send("Page.getLayoutMetrics"))


def validated_native_executable(playwright: _PlaywrightPath) -> Path:
    try:
        executable_path = Path(playwright.chromium.executable_path).resolve(strict=True)
        approved_root = (MANAGED_BROWSERS / "chromium-1228").resolve(strict=True)
    except (OSError, RuntimeError) as error:
        reason = "native zoom requires full managed Chromium revision 1228"
        raise BrowserRuntimeError(reason) from error
    if not executable_path.is_relative_to(approved_root):
        reason = "native zoom requires full managed Chromium revision 1228"
        raise BrowserRuntimeError(reason)
    return executable_path


def start_native_headless_context(
    playwright: Playwright,
    profile_path: Path,
    browser_environment: dict[str, str | float | bool] | None = None,
) -> tuple[BrowserContext, Path]:
    executable_path = validated_native_executable(playwright)
    context = playwright.chromium.launch_persistent_context(
        profile_path,
        headless=False,
        args=_FULL_CHROMIUM_NATIVE_HEADLESS_ARGS,
        env=browser_environment,
        viewport={"width": _PHYSICAL_WIDTH, "height": _PHYSICAL_HEIGHT},
    )
    return context, executable_path


def require_launch_created_page[T](context: _PagesOwner[T]) -> T:
    pages = context.pages
    if len(pages) != 1:
        reason = "native zoom requires exactly one launch-created page"
        raise BrowserRuntimeError(reason)
    return pages[0]


def capture_native_metrics(
    context: BrowserContext, executable_path: Path, profile_path: Path
) -> NativeZoomResult:
    page = require_launch_created_page(context)
    page.set_default_timeout(5_000)
    _ = page.goto(f"{UI_ORIGIN}/showcase", wait_until="load")
    page.get_by_role("heading", name="Primitive showcase", exact=True).wait_for()

    dom_hash_before = _dom_hash(page)
    cdp_session = context.new_cdp_session(page)
    try:
        metrics = _read_layout_metrics(cdp_session)
    finally:
        cdp_session.detach()
    state = _PageState.model_validate_json(evaluate_string(page, _PAGE_STATE_SCRIPT))
    dom_hash_after = _dom_hash(page)
    css_viewport = metrics.css_visual_viewport
    observation = ZoomObservation(
        layout_zoom=css_viewport.zoom,
        inner_width=state.inner_width,
        device_pixel_ratio=state.device_pixel_ratio,
        visual_viewport_scale=state.visual_viewport_scale,
        narrow_media=state.narrow_media,
        dom_hash=dom_hash_before,
    )
    return NativeZoomResult(
        profile_path=profile_path,
        executable_path=executable_path,
        chromium_revision=1228,
        launch_mode="full_chromium_new_headless",
        preference_path=_PREFERENCE_PATH,
        preference_level=_PREFERENCE_LEVEL,
        preference_sha256=sha256(NATIVE_PREFERENCES.encode()).hexdigest(),
        physical_width=state.outer_width,
        physical_height=state.outer_height,
        css_viewport_width=css_viewport.client_width,
        css_viewport_height=css_viewport.client_height,
        scroll_width=state.scroll_width,
        observation=observation,
        dom_hash_after=dom_hash_after,
    )


def assert_native_zoom_contract(result: NativeZoomResult) -> None:
    observation = result.observation
    if (result.physical_width, result.physical_height) != (_PHYSICAL_WIDTH, _PHYSICAL_HEIGHT):
        reason = "native zoom changed the physical viewport contract"
        raise BrowserRuntimeError(reason)
    if observation.layout_zoom != 2 or observation.device_pixel_ratio != 2:
        reason = "persistent profile did not apply native 200% zoom"
        raise BrowserRuntimeError(reason)
    if observation.inner_width != 640 or observation.visual_viewport_scale != 1:
        reason = "native zoom used a non-page scaling mechanism"
        raise BrowserRuntimeError(reason)
    if not observation.narrow_media:
        reason = "native zoom did not enter the narrow media layout"
        raise BrowserRuntimeError(reason)
    if result.scroll_width > observation.inner_width:
        reason = "native zoom produced page-level horizontal overflow"
        raise BrowserRuntimeError(reason)
    if observation.dom_hash != result.dom_hash_after:
        reason = "layout observation mutated the showcase DOM"
        raise BrowserRuntimeError(reason)
