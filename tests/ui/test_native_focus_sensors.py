import pytest

from .browser_checks import BrowserAssertionError, execute_script
from .browser_focus import keyboard_focus_count
from .browser_runtime import start_managed_browser, stop_managed_browser

pytestmark = pytest.mark.ui_fake


@pytest.mark.parametrize(
    ("markup", "observable"),
    [
        (
            """<div style='width:40px;overflow:hidden'>
<button style='width:100px'>Clipped</button></div>""",
            "clipped=1",
        ),
        ("<button style='opacity:0'>Hidden</button>", "hidden=1"),
        (
            """<button style='width:100px;height:40px'>Covered</button>
<div style='position:fixed;z-index:2;left:8px;top:8px;
width:100px;height:40px'>overlay</div>""",
            "covered=5",
        ),
    ],
)
def test_native_focus_sensor_rejects_adversarial_geometry(markup: str, observable: str) -> None:
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 640, "height": 450})
    page = context.new_page()
    try:
        page.set_content(markup)
        with pytest.raises(BrowserAssertionError, match=observable):
            _ = keyboard_focus_count(page)
    finally:
        try:
            context.close()
        finally:
            stop_managed_browser(managed)


def test_native_focus_sensor_rejects_an_uncontrolled_modal_escape() -> None:
    managed = start_managed_browser()
    context = managed.browser.new_context(viewport={"width": 640, "height": 450})
    page = context.new_page()
    try:
        page.set_content(
            "<button>Background</button><dialog><input><button>Cancel</button></dialog>"
        )
        execute_script(page, "() => document.querySelector('dialog').showModal()")
        with pytest.raises(BrowserAssertionError, match="escaped the open modal"):
            _ = keyboard_focus_count(page)
    finally:
        try:
            context.close()
        finally:
            stop_managed_browser(managed)
