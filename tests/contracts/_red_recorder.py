"""Private pytest plugin that records only safe result classifications."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import pytest

if TYPE_CHECKING:
    from pydantic import JsonValue

_REPORT_ENV = "NBLB_RED_REPORT_PATH"


@dataclass(frozen=True, slots=True)
class _Report:
    node_id: str
    phase: str
    outcome: str
    xfail: bool


class _RecorderState:
    __slots__: ClassVar[tuple[str, ...]] = (
        "collected",
        "collection_errors",
        "exception_classes",
        "reports",
        "xfail_nodes",
    )

    collected: list[str]
    xfail_nodes: set[str]
    reports: list[_Report]
    exception_classes: dict[tuple[str, str], str]
    collection_errors: int

    def __init__(self) -> None:
        self.collected = []
        self.xfail_nodes = set()
        self.reports = []
        self.exception_classes = {}
        self.collection_errors = 0


_STATE = _RecorderState()


def _was_xfail(report: pytest.TestReport) -> bool:
    try:
        _ = report.wasxfail
    except AttributeError:
        return False
    return True


def pytest_collection_finish(session: pytest.Session) -> None:
    """Capture collected identities and static xfail markers only."""
    _STATE.collected.extend(item.nodeid for item in session.items)
    _STATE.xfail_nodes.update(
        item.nodeid for item in session.items if item.get_closest_marker("xfail") is not None
    )


def pytest_collectreport(report: pytest.CollectReport) -> None:
    """Count collection errors without retaining their text."""
    if report.failed:
        _STATE.collection_errors += 1


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record call results plus non-passing setup/teardown classifications."""
    if report.when == "call" or report.outcome != "passed":
        _STATE.reports.append(
            _Report(
                node_id=report.nodeid,
                phase=report.when,
                outcome=report.outcome,
                xfail=report.nodeid in _STATE.xfail_nodes or _was_xfail(report),
            )
        )


def pytest_exception_interact[ResultT](
    node: pytest.Item | pytest.Collector,
    call: pytest.CallInfo[ResultT],
    report: pytest.CollectReport | pytest.TestReport,
) -> None:
    """Reduce an exception to its class name after pytest renders the report."""
    del node
    if isinstance(report, pytest.TestReport) and call.excinfo is not None:
        _STATE.exception_classes[(report.nodeid, report.when)] = call.excinfo.type.__name__


def pytest_sessionfinish(session: pytest.Session, exitstatus: int | pytest.ExitCode) -> None:
    """Atomically emit a message-free recorder payload."""
    del session
    destination_text = os.environ.get(_REPORT_ENV)
    if destination_text is None:
        return
    destination = Path(destination_text)
    temporary = destination.with_suffix(f"{destination.suffix}.tmp")
    collected_payload: list[JsonValue] = []
    collected_payload.extend(_STATE.collected)
    reports_payload: list[JsonValue] = [
        {
            "node_id": report.node_id,
            "phase": report.phase,
            "outcome": report.outcome,
            "failure_class": _STATE.exception_classes.get((report.node_id, report.phase)),
            "xfail": report.xfail,
        }
        for report in _STATE.reports
    ]
    payload: dict[str, JsonValue] = {
        "schema_version": 1,
        "collected": collected_payload,
        "collection_errors": _STATE.collection_errors,
        "reports": reports_payload,
        "exit_status": int(exitstatus),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _ = temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        _ = temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
