from hashlib import sha256
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui_fake

_AXE_VERSION = "4.12.1"
_AXE_SHA256 = "66a8aaa95a8b044a7fd74a5435873bf04ff65a1ca75567c921b7509742085a14"
_LICENSE_SHA256 = "af175b9d96ee93c21a036152e1b905b0b95304d4ae8c2c921c7609100ba8df7e"
_VENDOR_ROOT = Path(__file__).parent / "vendor" / f"axe-core-{_AXE_VERSION}"


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_official_axe_asset_is_pinned_for_offline_browser_qa() -> None:
    # Given: the versioned test-only vendor directory.
    script = _VENDOR_ROOT / "axe.min.js"
    license_file = _VENDOR_ROOT / "LICENSE"

    # When: its exact official release files are read without network access.
    script_digest = _digest(script)
    license_digest = _digest(license_file)

    # Then: version, bytes, license, and repository provenance are pinned.
    assert script_digest == _AXE_SHA256
    assert license_digest == _LICENSE_SHA256
    assert "axe v4.12.1" in script.read_text(encoding="utf-8")[:256]
    assert license_file.read_text(encoding="utf-8").startswith(
        "Mozilla Public License, version 2.0"
    )
    notice = Path("NOTICE.md").read_text(encoding="utf-8")
    assert "axe-core 4.12.1" in notice
    assert _AXE_SHA256 in notice
    assert "https://registry.npmjs.org/axe-core/-/axe-core-4.12.1.tgz" in notice
