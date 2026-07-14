from base64 import b64encode

from tests.ui import browser_png
from tests.ui.browser_png import (
    RgbPng,
    decode_rgb_png,
    encode_rgb_png,
    informative_row_coverage,
)


def _image(width: int, rows: tuple[tuple[int, int, int], ...]) -> RgbPng:
    return RgbPng(
        width=width,
        height=len(rows),
        rows=tuple(bytes(pixel * width) for pixel in rows),
    )


def test_rgb_png_round_trip_preserves_exact_pixels() -> None:
    image = _image(2, ((1, 2, 3), (4, 5, 6)))

    assert decode_rgb_png(encode_rgb_png(image)) == image


def test_information_coverage_detects_mostly_blank_native_bands() -> None:
    informative = tuple(bytes((value, 0, 0)) for _ in range(6) for value in (1, 2, 1, 2))
    blank = tuple(bytes((0, 0, 0)) for _ in range(26 * 4))
    image = RgbPng(width=1, height=128, rows=informative + blank)

    assert informative_row_coverage(image) == 6 / 32


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def send(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, params))
        if method == "Page.getLayoutMetrics":
            return {
                "contentSize": {"x": 0, "y": 0, "width": 1265, "height": 11471},
                "cssContentSize": {"x": 0, "y": 0, "width": 632, "height": 5735},
            }
        return {"data": b64encode(b"physical-document").decode()}

    def detach(self) -> None:
        pass


def test_native_viewport_capture_never_expands_the_layout_viewport() -> None:
    session = _RecordingSession()

    content = browser_png.capture_viewport_surface(session)

    assert content == b"physical-document"
    assert session.calls == [
        (
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "optimizeForSpeed": True,
            },
        ),
    ]


def test_native_viewport_tiles_cover_the_exact_physical_document() -> None:
    first = _image(4, ((1, 0, 0), (2, 0, 0), (3, 0, 0)))
    second = _image(4, ((30, 0, 0), (4, 0, 0), (5, 0, 0)))

    stitched = browser_png.stitch_viewport_tiles(
        (
            browser_png.ViewportTile(top=0, image=first),
            browser_png.ViewportTile(top=2, image=second),
        ),
        width=3,
        height=5,
    )

    assert stitched == _image(3, ((1, 0, 0), (2, 0, 0), (30, 0, 0), (4, 0, 0), (5, 0, 0)))
