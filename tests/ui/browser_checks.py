from pathlib import Path
from typing import ClassVar, Literal, Protocol, final, override

from playwright.sync_api import Page, Request
from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class AxeCounts(_StrictModel):
    serious: int
    critical: int
    network_requests: int = 0


class LayoutObservation(_StrictModel):
    inner_width: int
    scroll_width: int
    inner_height: int
    scroll_height: int


class StorageObservation(_StrictModel):
    local: int
    session: int
    cookies: int
    query: int


class ZoomObservation(_StrictModel):
    layout_zoom: float
    inner_width: int
    device_pixel_ratio: float
    visual_viewport_scale: float
    narrow_media: bool
    dom_hash: str


class ReducedMotionObservation(_StrictModel):
    media_matches: Literal[True]
    checked_styles: int = Field(gt=0)
    nonzero_transition_durations: Literal[0]
    nonzero_transition_delays: Literal[0]
    active_control_transform: Literal["none"]
    active_field_transform: Literal["none"]


class AdminDesktopObservation(_StrictModel):
    rail_width: Literal[224]
    main_grid_tracks: Literal[12]
    main_column_gap: Literal["24px"]
    main_padding_start: Literal["32px"]
    main_padding_end: Literal["32px"]
    disclosure_display: Literal["none"]
    rail_content_contained: Literal[True]
    main_children_full_span: Literal[True]
    dashboard_children_full_span: Literal[True]
    summary_cells: Literal[5]
    summary_grid_tracks: Literal[5]


class ShowcaseDesktopObservation(_StrictModel):
    rail_width: Literal[224]
    main_grid_tracks: Literal[12]
    main_column_gap: Literal["24px"]
    main_padding_start: Literal["32px"]
    main_padding_end: Literal["32px"]
    disclosure_display: Literal["none"]
    rail_content_contained: Literal[True]
    main_children_full_span: Literal[True]
    navigation_links: Literal[6]
    summary_cells: Literal[0]
    specimen_grid_tracks: Literal[3]
    status_grid_tracks: Literal[4]
    state_grid_tracks: Literal[3]


class _StringEvaluator(Protocol):
    def evaluate(self, expression: str) -> str: ...


class _ScriptEvaluator(Protocol):
    def evaluate(self, expression: str) -> None: ...


@final
class BrowserAssertionError(Exception):
    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason


def evaluate_string(page: _StringEvaluator, expression: str) -> str:
    return page.evaluate(expression)


def execute_script(page: _ScriptEvaluator, expression: str) -> None:
    page.evaluate(expression)


def axe_counts(page: Page, asset: Path) -> AxeCounts:
    network_requests = 0

    def count_request(request: Request) -> None:
        nonlocal network_requests
        del request
        network_requests += 1

    source = asset.read_text(encoding="utf-8")
    page.on("request", count_request)
    try:
        execute_script(page, source)
        start = "async () => {const result = await axe.run(document);return JSON.stringify({"
        serious = 'serious: result.violations.filter((item) => item.impact === "serious").length,'
        critical = 'critical: result.violations.filter((item) => item.impact === "critical").length'
        expression = f"{start}{serious}{critical}" + "});}"
        result = evaluate_string(
            page,
            expression,
        )
    finally:
        page.remove_listener("request", count_request)
    counts = AxeCounts.model_validate_json(result)
    return counts.model_copy(update={"network_requests": network_requests})


def layout_observation(page: Page) -> LayoutObservation:
    start = "() => JSON.stringify({inner_width: window.innerWidth,"
    width = "scroll_width: document.documentElement.scrollWidth,"
    height = "inner_height: window.innerHeight,"
    scroll = "scroll_height: document.documentElement.scrollHeight})"
    expression = f"{start}{width}{height}{scroll}"
    result = evaluate_string(
        page,
        expression,
    )
    return LayoutObservation.model_validate_json(result)


def storage_observation(page: Page) -> StorageObservation:
    start = "() => JSON.stringify({local: localStorage.length,"
    session = "session: sessionStorage.length,"
    cookies = "cookies: document.cookie.length,"
    query = "query: location.search.length})"
    expression = f"{start}{session}{cookies}{query}"
    result = evaluate_string(
        page,
        expression,
    )
    return StorageObservation.model_validate_json(result)


def reduced_motion_observation(page: Page) -> ReducedMotionObservation:
    expression = r"""() => {
const elements = [...document.querySelectorAll("*")];
const pseudos = [null, "::before", "::after"];
const styles = elements.flatMap((element) =>
  pseudos.map((pseudo) => getComputedStyle(element, pseudo))
);
const hasNonzeroTime = (value) =>
  value.split(",").some((part) => Number.parseFloat(part) !== 0);
return JSON.stringify({
  media_matches: matchMedia("(prefers-reduced-motion: reduce)").matches,
  checked_styles: styles.length,
  nonzero_transition_durations: styles.filter((style) =>
    hasNonzeroTime(style.transitionDuration)
  ).length,
  nonzero_transition_delays: styles.filter((style) =>
    hasNonzeroTime(style.transitionDelay)
  ).length,
  active_control_transform: getComputedStyle(
    document.querySelector('.control[data-state="active"]')
  ).transform,
  active_field_transform: getComputedStyle(
    document.querySelector('.field[data-state="active"] select')
  ).transform
});
}"""
    return ReducedMotionObservation.model_validate_json(evaluate_string(page, expression))


def admin_desktop_observation(page: Page) -> AdminDesktopObservation:
    expression = r"""() => {
const rail = document.querySelector(".rail");
const main = document.querySelector("main");
const disclosure = document.querySelector(".navigation-disclosure > summary");
const dashboard = document.querySelector("#dashboard");
const summary = document.querySelector(".summary-grid");
const style = getComputedStyle(main);
const fullSpan = (element) => {
  const item = getComputedStyle(element);
  return item.gridColumnStart === "1" && item.gridColumnEnd === "-1";
};
return JSON.stringify({
  rail_width: Math.round(rail.getBoundingClientRect().width),
  main_grid_tracks: style.gridTemplateColumns.split(/\s+/).length,
  main_column_gap: style.columnGap,
  main_padding_start: style.paddingInlineStart,
  main_padding_end: style.paddingInlineEnd,
  disclosure_display: getComputedStyle(disclosure).display,
  rail_content_contained: rail.contains(document.querySelector(".identity")) &&
    rail.contains(document.querySelector("nav")),
  main_children_full_span: [...main.children].filter((item) => !item.hidden).every(fullSpan),
  dashboard_children_full_span: [...dashboard.children]
    .filter((item) => !item.hidden).every(fullSpan),
  summary_cells: summary.children.length,
  summary_grid_tracks: getComputedStyle(summary).gridTemplateColumns.split(/\s+/).length
});
}"""
    return AdminDesktopObservation.model_validate_json(evaluate_string(page, expression))


def showcase_desktop_observation(page: Page) -> ShowcaseDesktopObservation:
    expression = r"""() => {
const rail = document.querySelector(".rail");
const main = document.querySelector("main");
const disclosure = document.querySelector(".navigation-disclosure > summary");
const style = getComputedStyle(main);
const tracks = (selector) =>
  getComputedStyle(document.querySelector(selector)).gridTemplateColumns.split(/\s+/).length;
const fullSpan = (element) => {
  const item = getComputedStyle(element);
  return item.gridColumnStart === "1" && item.gridColumnEnd === "-1";
};
return JSON.stringify({
  rail_width: Math.round(rail.getBoundingClientRect().width),
  main_grid_tracks: style.gridTemplateColumns.split(/\s+/).length,
  main_column_gap: style.columnGap,
  main_padding_start: style.paddingInlineStart,
  main_padding_end: style.paddingInlineEnd,
  disclosure_display: getComputedStyle(disclosure).display,
  rail_content_contained: rail.contains(document.querySelector(".identity")) &&
    rail.contains(document.querySelector("nav")),
  main_children_full_span: [...main.children].every(fullSpan),
  navigation_links: rail.querySelectorAll("nav a").length,
  summary_cells: document.querySelectorAll(".summary-grid > *").length,
  specimen_grid_tracks: tracks(".specimen-grid"),
  status_grid_tracks: tracks(".status-grid"),
  state_grid_tracks: tracks(".state-sections")
});
}"""
    return ShowcaseDesktopObservation.model_validate_json(evaluate_string(page, expression))


def clipboard_is_empty(page: Page) -> bool:
    result = evaluate_string(
        page,
        "async () => JSON.stringify((await navigator.clipboard.readText()).length === 0)",
    )
    return result == "true"


def script_flag_is_false(page: Page, flag: str) -> bool:
    result = evaluate_string(page, f"() => JSON.stringify(Boolean(globalThis[{flag!r}]))")
    return result == "false"


def assert_no_page_overflow(page: Page) -> None:
    layout = layout_observation(page)
    if layout.scroll_width > layout.inner_width:
        reason = "page has horizontal overflow"
        raise BrowserAssertionError(reason)


def focused_id(page: Page) -> str:
    return page.locator(":focus").get_attribute("id") or ""
