from collections.abc import Sequence
from hashlib import sha256
from json import dumps
from os import environ, getpid
from pathlib import Path
from sys import argv
from time import time_ns

import pytest

from nvidia_build_lb import pinned_runtime as _pinned_runtime

_RUNTIME_RECEIPT = _pinned_runtime.PINNED_RUNTIME
_NODES = pytest.StashKey[tuple[str, ...]]()
_STARTED = pytest.StashKey[int]()
_OUTCOMES: dict[str, str] = {}
_deselected_count = 0
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def selected_test_outcomes(config: pytest.Config) -> dict[str, str]:
    """Return every globally selected node with its safe outcome only."""
    nodes = config.stash.get(_NODES, ())
    return {node: _OUTCOMES.get(node, "not_run") for node in nodes}


def pytest_configure(config: pytest.Config) -> None:
    global _deselected_count  # noqa: PLW0603 - one pytest session owns this count.
    config.stash[_STARTED] = time_ns()
    _OUTCOMES.clear()
    _deselected_count = 0


def pytest_deselected(items: Sequence[pytest.Item]) -> None:
    """Count nodes removed by marker or selector expressions."""
    global _deselected_count  # noqa: PLW0603 - one pytest session owns this count.
    _deselected_count += len(items)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    session: pytest.Session,
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    del session
    config.stash[_NODES] = tuple(item.nodeid for item in items)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" or report.failed or report.skipped:
        _OUTCOMES[report.nodeid] = report.outcome


def _manifest_hash(paths: Sequence[Path]) -> str:
    digest = sha256()
    for path in sorted(paths):
        relative = path.relative_to(_REPOSITORY_ROOT).as_posix()
        file_hash = sha256(path.read_bytes()).hexdigest()
        digest.update(f"{file_hash}  {relative}\n".encode())
    return digest.hexdigest()


def _todo5_scope_hash() -> str:
    paths = [
        path
        for root in ("src/nvidia_build_lb", "tests/api", "tests/contracts")
        for path in (_REPOSITORY_ROOT / root).rglob("*.py")
    ]
    paths.extend(
        _REPOSITORY_ROOT / path for path in ("tests/conftest.py", "Makefile", "pyproject.toml")
    )
    return _manifest_hash(paths)


def _todo3_impact_scope_hash() -> str:
    paths = [
        path
        for root in ("src/nvidia_build_lb", "tests/nvidia_routing", "migrations")
        for path in (_REPOSITORY_ROOT / root).rglob("*")
        if path.is_file() and path.suffix in {".py", ".sql"}
    ]
    paths.append(_REPOSITORY_ROOT / "docs/ARCHITECTURE.md")
    return _manifest_hash(paths)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    raw_directory = environ.get("EVIDENCE_DIR")
    if raw_directory is None:
        return
    directory = Path(raw_directory).resolve()
    if not (directory / ".nblb-ui-fake-owner").is_file():
        return
    nodes = session.config.stash.get(_NODES, ())
    manifest = tuple({"node_id": node, "outcome": _OUTCOMES.get(node, "not_run")} for node in nodes)
    receipt = {
        "command": ["uv", "run", "pytest", *argv[1:]],
        "process_id": getpid(),
        "started_at_ns": session.config.stash[_STARTED],
        "finished_at_ns": time_ns(),
        "exit_code": int(exitstatus),
        "selected_node_count": len(nodes),
        "deselected_node_count": _deselected_count,
        "passed_node_count": sum(item["outcome"] == "passed" for item in manifest),
        "failed_node_count": sum(item["outcome"] == "failed" for item in manifest),
        "skipped_node_count": sum(item["outcome"] == "skipped" for item in manifest),
        "scope_sha256": {
            "todo5": _todo5_scope_hash(),
            "todo3_impact": _todo3_impact_scope_hash(),
        },
        "node_manifest": manifest,
    }
    _ = (directory / "command-receipt.json").write_text(
        dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
