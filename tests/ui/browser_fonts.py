from dataclasses import dataclass
from hashlib import sha256
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

_FONT_ASSET: Final = Path(__file__).parent / "vendor" / "noto-sans-kr" / "NotoSansKR-wght.ttf"
FONT_SHA256: Final = "194018e6b2b293a7964f037b25c0249ce1418bc9ab3c971060a03aa57861e252"
FONT_BYTE_COUNT: Final = 10_414_588


@dataclass(frozen=True, slots=True)
class BrowserFontEnvironment:
    temporary: TemporaryDirectory[str]
    variables: dict[str, str | float | bool]


def create_browser_font_environment() -> BrowserFontEnvironment:
    content = _FONT_ASSET.read_bytes()
    if len(content) != FONT_BYTE_COUNT or sha256(content).hexdigest() != FONT_SHA256:
        reason = "pinned Noto Sans KR browser QA asset does not match provenance"
        raise ValueError(reason)
    temporary = TemporaryDirectory(prefix="nblb-fontconfig-")
    root = Path(temporary.name)
    cache = root / "cache"
    cache.mkdir()
    config = root / "fonts.conf"
    config_text = (
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>'
        "<dir>/usr/share/fonts</dir>"
        f"<dir>{_FONT_ASSET.parent}</dir><cachedir>{cache}</cachedir>"
        "</fontconfig>"
    )
    _ = config.write_text(config_text, encoding="utf-8")
    variables: dict[str, str | float | bool] = dict(environ)
    variables["FONTCONFIG_FILE"] = str(config)
    variables["XDG_CACHE_HOME"] = str(cache)
    return BrowserFontEnvironment(temporary=temporary, variables=variables)
