"""Maintain the reviewable product-source size and generated-asset contracts."""

from pathlib import Path

from scripts.build_admin_asset import OUTPUT, render_admin_asset

_ROOT = Path(__file__).parents[2]
_PRODUCT_ROOTS = (_ROOT / "src" / "nvidia_build_lb", _ROOT / "frontend")
_PRODUCT_SUFFIXES = frozenset({".py", ".js", ".css", ".html"})


def test_product_source_files_are_at_most_250_lines() -> None:
    sources = tuple(
        path
        for root in _PRODUCT_ROOTS
        for path in root.rglob("*")
        if path.is_file() and path.suffix in _PRODUCT_SUFFIXES
    )

    oversized = {
        str(path.relative_to(_ROOT)): len(path.read_text(encoding="utf-8").splitlines())
        for path in sources
        if len(path.read_text(encoding="utf-8").splitlines()) > 250
    }

    assert oversized == {}


def test_served_admin_module_exactly_matches_ordered_fragments() -> None:
    assert OUTPUT.read_text(encoding="utf-8") == render_admin_asset()
