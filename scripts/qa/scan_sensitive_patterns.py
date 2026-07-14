"""Scan the candidate Git surface for exact product credential shapes."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from scripts.qa.source_manifest import build_manifest, read_candidate_entry

_PATTERNS = (
    re.compile(rb"nblb_admin_[0-9a-f]{64}"),
    re.compile(rb"nblb_ds_[0-9a-f]{64}"),
    re.compile(rb"nvapi-(?!(?:synthetic|contract)-)[A-Za-z0-9_-]{20,}"),
)
_PUBLIC_EXAMPLE_TOKEN = b"nblb_ds_" + (b"a" * 64)


class _Arguments(argparse.Namespace):
    root: Path = Path()


def matching_paths(root: Path) -> tuple[str, ...]:
    """Return only safe path names, never matching bytes."""
    manifest = build_manifest(root)
    matches: list[str] = []
    for entry in manifest["entries"]:
        if entry["type"] != "regular":
            continue
        payload = read_candidate_entry(root, entry["path"]).payload.replace(
            _PUBLIC_EXAMPLE_TOKEN,
            b"public-example-token",
        )
        if any(pattern.search(payload) is not None for pattern in _PATTERNS):
            matches.append(entry["path"])
    return tuple(matches)


def main() -> None:
    """Fail closed while reporting only candidate path names."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("root", type=Path)
    arguments = parser.parse_args(namespace=_Arguments())
    matches = matching_paths(arguments.root)
    if matches:
        _ = sys.stderr.write("credential_pattern_failed:" + ",".join(matches) + "\n")
        raise SystemExit(1)
    _ = sys.stdout.write("credential_pattern_passed\n")


if __name__ == "__main__":
    main()
