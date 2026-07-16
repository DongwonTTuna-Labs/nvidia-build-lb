from typing import ClassVar, Final

from playwright.sync_api import Page
from pydantic import BaseModel, ConfigDict

from .browser_checks import (
    BrowserAssertionError,
    evaluate_string,
    execute_script,
)

_FOCUSABLE_COUNT: Final = r"""() => JSON.stringify([...document.querySelectorAll(
'a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),summary,[tabindex="0"]'
)].filter((item) => {
  const modal = document.querySelector('dialog:modal');
  return item.getClientRects().length > 0 && (!modal || modal.contains(item));
}).length)"""
_FOCUS_STATE: Final = r"""() => {
const modal = document.querySelector('dialog:modal');
const focusable = [...document.querySelectorAll(
  'a[href],button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),summary,[tabindex="0"]'
)].filter((item) => item.getClientRects().length > 0 && (!modal || modal.contains(item)));
const active = document.activeElement;
const rect = active.getBoundingClientRect();
const style = getComputedStyle(active);
const outline = Number.parseFloat(style.outlineWidth) || 0;
const offset = Number.parseFloat(style.outlineOffset) || 0;
const edge = outline + Math.max(0, offset);
const union = {left:rect.left-edge, top:rect.top-edge,
  right:rect.right+edge, bottom:rect.bottom+edge};
const viewport = window.visualViewport;
const inside = union.left >= viewport.offsetLeft && union.top >= viewport.offsetTop &&
  union.right <= viewport.offsetLeft + viewport.width &&
  union.bottom <= viewport.offsetTop + viewport.height;
let clipped_count = 0, hidden_count = 0;
for (let item = active; item; item = item.parentElement) {
  const itemStyle = getComputedStyle(item);
  if (itemStyle.display === "none" || itemStyle.visibility === "hidden" ||
      Number.parseFloat(itemStyle.opacity) === 0) hidden_count += 1;
  if (item !== active && /(hidden|clip|scroll|auto)/.test(
      `${itemStyle.overflowX} ${itemStyle.overflowY}`)) {
    const clip = item.getBoundingClientRect();
    if (union.left < clip.left || union.top < clip.top ||
        union.right > clip.right || union.bottom > clip.bottom) clipped_count += 1;
  }
}
const points = [[rect.left+1,rect.top+1],[rect.right-1,rect.top+1],
  [rect.left+1,rect.bottom-1],[rect.right-1,rect.bottom-1],
  [rect.left+rect.width/2,rect.top+rect.height/2]];
const covered_count = points.filter(([x,y]) => {
  const hit = document.elementFromPoint(x,y); return hit !== active && !active.contains(hit);
}).length;
const overlaps = [...document.querySelectorAll("*")].some((item) => {
  if (item === active || item.contains(active) || active.contains(item)) return false;
  const position = getComputedStyle(item).position;
  if (position !== "fixed" && position !== "sticky") return false;
  const other = item.getBoundingClientRect();
  return other.left < union.right && other.right > union.left &&
    other.top < union.bottom && other.bottom > union.top;
});
return JSON.stringify({
  index: focusable.indexOf(active), inside, clipped_count, hidden_count, covered_count, overlaps,
  modal_open: Boolean(modal), inside_modal: !modal || modal.contains(active),
  left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
  viewport_width: viewport.width, viewport_height: viewport.height
});
}"""


class _FocusState(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)

    index: int
    inside: bool
    clipped_count: int
    hidden_count: int
    covered_count: int
    overlaps: bool
    modal_open: bool
    inside_modal: bool
    left: float
    top: float
    right: float
    bottom: float
    viewport_width: float
    viewport_height: float


def keyboard_focus_count(page: Page) -> int:
    count = int(evaluate_string(page, _FOCUSABLE_COUNT))
    execute_script(page, "() => document.activeElement.blur()")
    observed: set[int] = set()
    for _ in range(count + 2):
        page.keyboard.press("Tab")
        state = _FocusState.model_validate_json(evaluate_string(page, _FOCUS_STATE))
        if state.index == -1:
            if state.modal_open:
                reason = "native focus escaped the open modal"
                raise BrowserAssertionError(reason)
            continue
        if state.index in observed:
            break
        if (
            not state.inside
            or not state.inside_modal
            or state.clipped_count
            or state.hidden_count
            or state.covered_count
            or state.overlaps
        ):
            reason = (
                f"native focus {state.index} geometry failed: inside={state.inside}, "
                f"clipped={state.clipped_count}, hidden={state.hidden_count}, "
                f"covered={state.covered_count}, overlaps={state.overlaps}, "
                f"rect={state.left},{state.top},{state.right},{state.bottom}, "
                f"viewport={state.viewport_width}x{state.viewport_height}"
            )
            raise BrowserAssertionError(reason)
        observed.add(state.index)
    if observed != set(range(count)):
        reason = "native natural keyboard traversal missed an enabled control"
        raise BrowserAssertionError(reason)
    return count
