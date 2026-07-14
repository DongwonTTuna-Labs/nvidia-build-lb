from hashlib import sha256
from pathlib import Path

import pytest

from .browser_fonts import FONT_BYTE_COUNT, FONT_SHA256

pytestmark = pytest.mark.ui_fake


def test_official_noto_sans_kr_asset_is_pinned_for_browser_only_qa() -> None:
    # Given: the test-only Korean font asset used without a browser request or product route.
    asset = Path(__file__).parent / "vendor" / "noto-sans-kr" / "NotoSansKR-wght.ttf"

    # When: its current provenance is measured from bytes.
    content = asset.read_bytes()

    # Then: only the reviewed Google Fonts artifact can enter the temporary Fontconfig.
    assert len(content) == FONT_BYTE_COUNT
    assert sha256(content).hexdigest() == FONT_SHA256
