"""Reject every non-local GitHub Action reference not pinned to a full commit."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)")
_REMOTE_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


class _Arguments(argparse.Namespace):
    workflow_root: Path = Path()


def invalid_action_references(workflow_root: Path) -> tuple[str, ...]:
    """Return safe path and line locations for mutable remote references."""
    invalid: list[str] = []
    for path in sorted(workflow_root.glob("*.yml")):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _USES.match(line)
            if match is None:
                continue
            reference = match.group(1)
            if reference.startswith("./"):
                continue
            if _REMOTE_PIN.fullmatch(reference) is None:
                invalid.append(f"{path.name}:{line_number}")
    return tuple(invalid)


def main() -> None:
    """Exit nonzero without echoing a rejected action reference."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("workflow_root", type=Path)
    arguments = parser.parse_args(namespace=_Arguments())
    invalid = invalid_action_references(arguments.workflow_root)
    if invalid:
        _ = sys.stderr.write("action_pin_failed:" + ",".join(invalid) + "\n")
        raise SystemExit(1)
    _ = sys.stdout.write("action_pin_passed\n")


if __name__ == "__main__":
    main()
