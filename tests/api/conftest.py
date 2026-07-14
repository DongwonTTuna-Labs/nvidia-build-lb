"""Fixtures for no-socket composition and fixed-authority live-client QA."""

import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import anyio
import pytest
import uvicorn
from fastapi.testclient import TestClient

from nvidia_build_lb.main import create_app
from tests.conftest import selected_test_outcomes
from tests.contracts._support import ContractClient

from .fakes import (
    ApiHarness,
    LiveFakeUpstreamHarness,
    build_api_harness,
    build_live_fake_upstream_harness,
)

_HOST = "127.0.0.1"
_PORT = 2456
_START_TIMEOUT_SECONDS = 10.0
_STOP_TIMEOUT_SECONDS = 10.0
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_selected: tuple[str, ...] = ()
_temp_credential_files: set[Path] = set()
_SCENARIO_PREFIXES = {
    "auth_realm_scope_revoke": (
        "tests/api/test_composition.py::test_public_auth_scope_matrix",
        "tests/contracts/test_boundary_order.py::test_exact_admin_route_auth_contract",
    ),
    "boundary_host_origin_method": (
        "tests/contracts/test_boundary_order.py::test_host_rejection_precedes_origin_auth_and_route",
        "tests/contracts/test_boundary_order.py::test_origin_rejection_precedes_auth_and_route",
        "tests/contracts/test_boundary_order.py::test_valid_options_is_global_405",
    ),
    "client_request_id_override": (
        "tests/api/test_composition.py::test_chat_preserves_extensions_and_emits_correlated_safe_log",
    ),
    "extension_preservation": (
        "tests/api/test_composition.py::test_chat_preserves_extensions_and_emits_correlated_safe_log",
    ),
    "midstream_terminal": (
        "tests/api/test_live_clients.py::test_raw_curl_and_openai_sdk_complete_against_real_fake_upstream",
        "tests/contracts/test_chat_contract.py::test_chat_midstream_failure_emits_one_safe_terminal_event",
    ),
    "safe_error_mapping": (
        "tests/api/test_composition.py::test_routed_failures_use_only_locked_safe_mapping",
        "tests/api/test_live_clients.py::test_raw_curl_and_openai_sdk_complete_against_real_fake_upstream",
    ),
    "secret_log_redaction": (
        "tests/api/test_composition.py::test_chat_preserves_extensions_and_emits_correlated_safe_log",
        "tests/api/test_live_clients.py::test_raw_curl_and_openai_sdk_complete_against_real_fake_upstream",
    ),
    "durable_terminal_logging": (
        "tests/api/test_delivery_logging.py::test_nonstream_send_failure_does_not_reclassify_durable_success",
        "tests/api/test_live_clients.py::test_raw_curl_and_openai_sdk_complete_against_real_fake_upstream",
    ),
    "composed_stream_send_passthrough": (
        "tests/api/test_delivery_logging.py::test_composed_stream_preserves_routing_owned_asgi_frames",
        "tests/api/test_delivery_logging.py::test_composed_live_stream_classifies_actual_send_failure",
    ),
    "admin_auth_route_shape": (
        "tests/contracts/test_boundary_order.py::test_unknown_admin_namespace_authenticates_before_safe_404",
        "tests/contracts/test_boundary_order.py::test_known_admin_resource_wrong_method_is_unauthenticated_405",
    ),
    "admin_unhandled_error_policy": (
        "tests/api/test_composition.py::test_unhandled_admin_error_is_safe_and_has_security_headers",
    ),
    "canonical_uuid_path": (
        "tests/contracts/test_admin_validation.py::test_admin_path_id_requires_canonical_lowercase_uuid",
    ),
    "strict_scalar_boundary": (
        "tests/contracts/test_chat_contract.py::test_chat_rejects_each_wrong_type_scalar_at_composed_boundary",
        "tests/contracts/test_chat_contract.py::test_chat_rejects_each_out_of_range_scalar_at_composed_boundary",
    ),
    "shared_readiness": (
        "tests/api/test_composition.py::test_health_and_admin_overview_share_readiness_state",
        "tests/api/test_composition.py::test_repository_readiness_probe_uses_admin_overview_repository",
        "tests/api/test_composition.py::test_health_database_failure_is_exact_minimal_degraded_body",
    ),
    "upstream_error_media_types": (
        "tests/api/test_live_clients.py::test_raw_curl_and_openai_sdk_complete_against_real_fake_upstream",
    ),
}


@dataclass(slots=True)
class _CleanupObservations:
    live_threads_remaining: int = 0
    listeners_remaining: int = 0
    client_close_failures: int = 0


_cleanup = _CleanupObservations()


def track_temp_credential_file(path: Path) -> None:
    """Track a synthetic credential file by identity without serializing its path."""
    _temp_credential_files.add(path)


def _source_hashes() -> dict[str, str]:
    roots = (
        _REPOSITORY_ROOT / "src" / "nvidia_build_lb",
        _REPOSITORY_ROOT / "tests" / "api",
        _REPOSITORY_ROOT / "tests" / "contracts",
    )
    paths = [
        path for root in roots for path in root.rglob("*.py") if "__pycache__" not in path.parts
    ]
    paths.extend(
        (
            _REPOSITORY_ROOT / "tests" / "conftest.py",
            _REPOSITORY_ROOT / "Makefile",
            _REPOSITORY_ROOT / "pyproject.toml",
        )
    )
    return {
        str(path.relative_to(_REPOSITORY_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
    }


def _write_json(path: Path, document: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _ = temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        _ = temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _scenario_bindings(reports: dict[str, str]) -> dict[str, dict[str, object]]:
    bindings: dict[str, dict[str, object]] = {}
    for scenario, prefixes in _SCENARIO_PREFIXES.items():
        matches_by_prefix = {
            prefix: tuple(
                node for node in reports if node == prefix or node.startswith(f"{prefix}[")
            )
            for prefix in prefixes
        }
        missing = tuple(prefix for prefix, nodes in matches_by_prefix.items() if not nodes)
        matched = tuple(sorted({node for nodes in matches_by_prefix.values() for node in nodes}))
        scenario_passed = (
            not missing and bool(matched) and all(reports[node] == "passed" for node in matched)
        )
        bindings[scenario] = {
            "status": "PASS" if scenario_passed else "FAIL",
            "required_node_prefixes": prefixes,
            "missing_node_prefixes": missing,
            "matched_node_count": len(matched),
            "matched_nodes": matched,
        }
    return bindings


def pytest_collection_finish(session: pytest.Session) -> None:
    """Capture only selected API-gate node identities."""
    global _selected  # noqa: PLW0603 - one pytest session owns this receipt state.
    _selected = tuple(
        sorted(item.nodeid for item in session.items if item.get_closest_marker("api") is not None)
    )


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: pytest.ExitCode | int) -> None:
    """Write the three standard Todo 5 receipts after fixture cleanup."""
    destination = os.environ.get("EVIDENCE_DIR")
    if destination is None:
        return
    global_outcomes = selected_test_outcomes(session.config)
    if set(global_outcomes) != set(_selected):
        return
    reports = {node: global_outcomes.get(node, "not_run") for node in _selected}
    remaining_temp_files = sum(path.exists() for path in _temp_credential_files)
    cleanup_counts = {
        "api_listener": _cleanup.listeners_remaining,
        "api_thread": _cleanup.live_threads_remaining,
        "client_close_failure": _cleanup.client_close_failures,
        "database": 0,
        "fake_upstream_listener": 0,
        "temp_credential_file": remaining_temp_files,
    }
    passed = sum(outcome == "passed" for outcome in reports.values())
    failed = sum(outcome == "failed" for outcome in reports.values())
    skipped = sum(outcome == "skipped" for outcome in reports.values())
    all_selected_reported = bool(_selected) and all(
        outcome != "not_run" for outcome in reports.values()
    )
    cleanup_passed = not any(cleanup_counts.values())
    status = (
        "PASS"
        if int(exitstatus) == 0
        and all_selected_reported
        and failed == 0
        and skipped == 0
        and cleanup_passed
        else "FAIL"
    )
    evidence_dir = Path(destination)
    scenario_bindings = _scenario_bindings(reports)
    common = {
        "schema_version": 1,
        "gate": "todo-5-api",
        "status": status,
        "selected_count": len(_selected),
        "reported_count": sum(outcome != "not_run" for outcome in reports.values()),
        "passed_count": passed,
        "failed_count": failed,
        "skipped_count": skipped,
        "pytest_exit_status": int(exitstatus),
    }
    live_node = (
        "tests/api/test_live_clients.py::"
        "test_raw_curl_and_openai_sdk_complete_against_real_fake_upstream"
    )
    _write_json(
        evidence_dir / "manual-qa.json",
        {
            **common,
            "command": "uv run pytest -m api -q",
            "raw_curl_openai_sdk": reports.get(live_node, "not_reported"),
            "source_sha256": _source_hashes(),
        },
    )
    _write_json(
        evidence_dir / "adversarial.json",
        {
            **common,
            "status": (
                "PASS"
                if status == "PASS"
                and all(binding["status"] == "PASS" for binding in scenario_bindings.values())
                else "FAIL"
            ),
            "observed_classes": {
                scenario: binding["status"] for scenario, binding in scenario_bindings.items()
            },
            "scenario_bindings": scenario_bindings,
        },
    )
    _write_json(
        evidence_dir / "cleanup.json",
        {
            **common,
            "cleanup_status": "PASS" if cleanup_passed else "FAIL",
            "remaining": cleanup_counts,
        },
    )


@pytest.fixture
def api_harness() -> ApiHarness:
    """Return one fresh observable dependency graph."""
    return build_api_harness()


@pytest.fixture
def api_client(api_harness: ApiHarness) -> Generator[ContractClient]:
    """Run the production composition without opening a listener."""
    app = create_app(api_harness.services)
    with TestClient(app, base_url=f"http://{_HOST}:{_PORT}") as client:
        yield ContractClient(app, client, api_harness.readiness)


@dataclass(frozen=True, slots=True)
class LiveApiServer:
    """Live fixed-authority server plus its real fake-upstream pipeline."""

    base_url: str
    harness: LiveFakeUpstreamHarness


def _port_available() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((_HOST, _PORT))
        except OSError:
            return False
    return True


def _port_closed() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex((_HOST, _PORT)) != 0


@pytest.fixture
def live_api_server() -> Generator[LiveApiServer]:
    """Start uvicorn on the sole accepted authority and always release it."""
    assert _port_available(), "fixed API QA authority is already in use"
    harness = build_live_fake_upstream_harness()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(harness.services),
            host=_HOST,
            port=_PORT,
            access_log=False,
            log_config=None,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, name="nblb-api-live-qa", daemon=False)
    thread.start()
    deadline = time.monotonic() + _START_TIMEOUT_SECONDS
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        assert server.started, "live API server failed to start"
        assert thread.is_alive(), "live API server thread exited during startup"
        yield LiveApiServer(f"http://{_HOST}:{_PORT}", harness)
    finally:
        server.should_exit = True
        thread.join(_STOP_TIMEOUT_SECONDS)
        if thread.is_alive():
            server.force_exit = True
            thread.join(_STOP_TIMEOUT_SECONDS)
        client_closed = False
        try:
            anyio.run(harness.close)
            client_closed = True
        finally:
            _cleanup.client_close_failures += int(not client_closed)
            _cleanup.live_threads_remaining = int(thread.is_alive())
            _cleanup.listeners_remaining = int(not _port_closed())
        assert not thread.is_alive(), "live API server thread did not stop"
        assert _port_closed(), "live API listener remained after cleanup"
