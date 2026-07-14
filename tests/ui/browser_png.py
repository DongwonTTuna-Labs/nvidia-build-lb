from base64 import b64decode
from dataclasses import dataclass
from hashlib import sha256
from struct import pack
from typing import ClassVar, Never, Protocol
from zlib import compress, crc32, decompress

from playwright.sync_api import Page
from pydantic import BaseModel, ConfigDict, Field

from .browser_checks import evaluate_string, execute_script

_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _fail(reason: str) -> Never:
    raise ValueError(reason)


@dataclass(frozen=True, slots=True)
class RgbPng:
    width: int
    height: int
    rows: tuple[bytes, ...]

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or len(self.rows) != self.height:
            _fail("invalid RGB PNG geometry")
        if any(len(row) != self.width * 3 for row in self.rows):
            _fail("invalid RGB PNG row width")


@dataclass(frozen=True, slots=True)
class ViewportTile:
    top: int
    image: RgbPng


class _JsonModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class NativeCaptureMetrics(_JsonModel):
    css_height: int = Field(alias="cssHeight")
    css_inner_width: float = Field(alias="cssInnerWidth")
    css_inner_height: float = Field(alias="cssInnerHeight")
    dpr: float
    physical_width: int = Field(alias="physicalWidth")
    physical_height: int = Field(alias="physicalHeight")


class _PhysicalContentSize(_JsonModel):
    x: float
    y: float
    width: float
    height: float


class _LayoutMetrics(_JsonModel):
    content_size: _PhysicalContentSize = Field(alias="contentSize")


class _ScreenshotPayload(_JsonModel):
    data: str


class _CdpScreenshotSession(Protocol):
    def send(self, method: str, params: dict[str, object]) -> dict[str, object]: ...

    def detach(self) -> None: ...


def capture_viewport_surface(session: _CdpScreenshotSession) -> bytes:
    payload = _ScreenshotPayload.model_validate(
        session.send(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "optimizeForSpeed": True,
            },
        )
    )
    return b64decode(payload.data, validate=True)


def physical_content_size(session: _CdpScreenshotSession) -> _PhysicalContentSize:
    raw_layout = session.send("Page.getLayoutMetrics", {})
    return _LayoutMetrics.model_validate(
        {"contentSize": raw_layout.get("contentSize")}
    ).content_size


def stitch_viewport_tiles(tiles: tuple[ViewportTile, ...], width: int, height: int) -> RgbPng:
    if not tiles or width <= 0 or height <= 0:
        _fail("native tile geometry is empty")
    rows: list[bytes | None] = [None] * height
    for tile in tiles:
        if tile.top < 0 or tile.image.width < width:
            _fail("native tile is outside the physical document")
        for source_y, row in enumerate(tile.image.rows):
            target_y = tile.top + source_y
            if target_y >= height:
                break
            rows[target_y] = row[: width * 3]
    if any(row is None for row in rows):
        _fail("native viewport tiles left a document gap")
    return RgbPng(width=width, height=height, rows=tuple(row for row in rows if row is not None))


def _chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = crc32(kind)
    checksum = crc32(payload, checksum) & 0xFFFFFFFF
    return pack(">I", len(payload)) + kind + payload + pack(">I", checksum)


def encode_rgb_png(image: RgbPng) -> bytes:
    header = pack(">IIBBBBB", image.width, image.height, 8, 2, 0, 0, 0)
    pixels = b"".join(b"\0" + row for row in image.rows)
    return (
        _SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", compress(pixels))
        + _chunk(b"IEND", b"")
    )


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
    return (left, above, upper_left)[distances.index(min(distances))]


def _unfilter(scanline: bytes, prior: bytes, filter_type: int) -> bytes:
    if filter_type == 0:
        return scanline
    if filter_type == 2:
        if not prior:
            return scanline
        return bytes((value + above) & 0xFF for value, above in zip(scanline, prior, strict=True))
    result = bytearray(len(scanline))
    for index, value in enumerate(scanline):
        left = result[index - 3] if index >= 3 else 0
        above = prior[index] if prior else 0
        upper_left = prior[index - 3] if prior and index >= 3 else 0
        predictor = {
            0: 0,
            1: left,
            2: above,
            3: (left + above) // 2,
            4: _paeth(left, above, upper_left),
        }.get(filter_type)
        if predictor is None:
            _fail("unsupported PNG filter")
        result[index] = (value + predictor) & 0xFF
    return bytes(result)


def decode_rgb_png(content: bytes) -> RgbPng:
    if not content.startswith(_SIGNATURE):
        _fail("invalid PNG signature")
    offset = len(_SIGNATURE)
    width = height = 0
    compressed = bytearray()
    while offset < len(content):
        length = int.from_bytes(content[offset : offset + 4], byteorder="big")
        kind = content[offset + 4 : offset + 8]
        payload = content[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width = int.from_bytes(payload[0:4], byteorder="big")
            height = int.from_bytes(payload[4:8], byteorder="big")
            depth, color, compression, filtering, interlace = payload[8:13]
            if (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0):
                _fail("PNG is not non-interlaced 8-bit RGB")
        elif kind == b"IDAT":
            compressed.extend(payload)
        offset += length + 12
    raw = decompress(bytes(compressed))
    stride = width * 3
    rows: list[bytes] = []
    cursor = 0
    prior = b""
    for _ in range(height):
        filter_type = raw[cursor]
        scanline = raw[cursor + 1 : cursor + 1 + stride]
        prior = _unfilter(scanline, prior, filter_type)
        rows.append(prior)
        cursor += stride + 1
    if cursor != len(raw):
        _fail("PNG raster length changed")
    return RgbPng(width=width, height=height, rows=tuple(rows))


def capture_native_document_png(page: Page) -> bytes:
    metrics_script = """() => JSON.stringify({
cssHeight:document.documentElement.scrollHeight,
cssInnerWidth:window.innerWidth,
cssInnerHeight:window.innerHeight,
dpr:window.devicePixelRatio,
physicalWidth:Math.round(window.innerWidth*window.devicePixelRatio),
physicalHeight:Math.round(document.documentElement.scrollHeight*window.devicePixelRatio)
})"""
    metrics = NativeCaptureMetrics.model_validate_json(evaluate_string(page, metrics_script))
    capture_state_script = """() => JSON.stringify({
innerWidth:window.innerWidth,
innerHeight:window.innerHeight,
dpr:window.devicePixelRatio,
scrollWidth:document.documentElement.scrollWidth,
scrollHeight:document.documentElement.scrollHeight,
visualWidth:window.visualViewport.width,
visualHeight:window.visualViewport.height,
visualScale:window.visualViewport.scale,
scrollX:window.scrollX,
scrollY:window.scrollY
})"""
    dom_before = sha256(page.content().encode()).digest()
    capture_state_before = evaluate_string(page, capture_state_script)
    session = page.context.new_cdp_session(page)
    try:
        physical = physical_content_size(session)
        if physical.x != 0 or physical.y != 0:
            _fail("native physical content origin changed")
        width = round(physical.width)
        height = round(physical.height)
        if not 0 < width <= metrics.physical_width:
            _fail("native physical content exceeds the measured inner width")
        if abs(height - metrics.physical_height) > 1:
            _fail("native physical content height disagrees with the measured document")
        original_x = float(evaluate_string(page, "() => JSON.stringify(window.scrollX)"))
        original_y = float(evaluate_string(page, "() => JSON.stringify(window.scrollY)"))
        tiles: list[ViewportTile] = []
        seen_tops: set[int] = set()
        target_y = 0.0
        try:
            while True:
                execute_script(page, f"() => window.scrollTo(0, {target_y})")
                actual_y = float(evaluate_string(page, "() => JSON.stringify(window.scrollY)"))
                top = round(actual_y * metrics.dpr)
                if top in seen_tops:
                    _fail("native viewport tile scroll made no progress")
                seen_tops.add(top)
                image = decode_rgb_png(capture_viewport_surface(session))
                expected_tile_height = round(metrics.css_inner_height * metrics.dpr)
                if image.width < width or image.height != expected_tile_height:
                    _fail("native viewport surface geometry changed")
                tiles.append(ViewportTile(top=top, image=image))
                if top + image.height >= height:
                    break
                target_y = actual_y + metrics.css_inner_height
        finally:
            execute_script(page, f"() => window.scrollTo({original_x}, {original_y})")
        content = encode_rgb_png(stitch_viewport_tiles(tuple(tiles), width, height))
    finally:
        session.detach()
    if sha256(page.content().encode()).digest() != dom_before:
        _fail("native document capture mutated the DOM")
    if evaluate_string(page, capture_state_script) != capture_state_before:
        _fail("native document capture mutated the layout viewport")
    return content


def informative_row_coverage(image: RgbPng, bands: int = 32) -> float:
    informative = 0
    for band in range(bands):
        top = band * image.height // bands
        bottom = max(top + 1, (band + 1) * image.height // bands)
        sampled: set[bytes] = set()
        row_step = max(1, (bottom - top) // 4)
        pixel_step = max(1, image.width // 128)
        for row in image.rows[top:bottom:row_step]:
            for offset in range(0, image.width, pixel_step):
                sampled.add(row[offset * 3 : offset * 3 + 3])
                if len(sampled) > 1:
                    break
            if len(sampled) > 1:
                break
        informative += len(sampled) > 1
    return informative / bands
