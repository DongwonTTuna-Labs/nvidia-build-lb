"""Executable state matrix for the host-owned operator probe."""

import subprocess
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from typing import ClassVar, override
from uuid import UUID

import pytest
from scripts.operator_readiness_probe import (
    OperatorProbeMode,
    container_generation,
    dashboard_probe_confirmed,
    legacy_overview_confirmed,
    legacy_runtime_confirmed,
    operator_http_request,
    operator_readiness_probe_confirmed,
)

from nvidia_build_lb.admin.schemas import (
    AdminDashboardRead,
    AdminOperatorReadinessRead,
    CapacityBlocker,
    HealthState,
    LedgerStatus,
    OverviewStatus,
    ReadinessCause,
    RuntimeState,
    UpstreamRoutingState,
)
from tests.ui.fake_admin_state import FakeAdminState

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/operator_readiness_probe.py"
_ISOLATED_PYTHON = Path("/usr/bin/python3")
_TOKEN = "nblb_admin_" + ("a" * 64)
_OVERSIZED_BODY_BYTES = 2 * 1024 * 1024 + 1


class _LegacyOverviewHandler(BaseHTTPRequestHandler):
    expected_host: ClassVar[str] = ""
    readiness_payload: ClassVar[bytes | None] = None
    overview_payload: ClassVar[bytes] = b""
    observed_paths: ClassVar[list[str]] = []
    observed_hosts: ClassVar[list[str | None]] = []
    overview_calls: ClassVar[int] = 0

    def _respond(self, status: int, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        _ = self.wfile.write(payload)

    def do_GET(self) -> None:
        type(self).observed_paths.append(self.path)
        type(self).observed_hosts.append(self.headers.get("Host"))
        if self.headers.get("Authorization") != "Bearer " + _TOKEN:
            self._respond(401, b"{}")
        elif self.headers.get("Host") != type(self).expected_host:
            self._respond(403, b"{}")
        elif self.path == "/admin/api/v1/operator-readiness":
            readiness = type(self).readiness_payload
            if readiness is None:
                self._respond(404, b"{}")
            else:
                self._respond(200, readiness)
        elif self.path == "/admin/api/v1/overview":
            type(self).overview_calls += 1
            self._respond(200, type(self).overview_payload)
        else:
            self._respond(404, b"{}")

    @override
    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _SlowBodyHandler(BaseHTTPRequestHandler):
    expected_host: ClassVar[str] = ""

    def do_GET(self) -> None:
        if self.headers.get("Host") != type(self).expected_host:
            self.send_error(403)
            return
        payload = b"slow"
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        for byte in payload:
            try:
                _ = self.wfile.write(bytes((byte,)))
                self.wfile.flush()
            except OSError:
                return
            sleep(0.75)

    @override
    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _OversizedBodyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        try:
            _ = self.wfile.write(b"x" * _OVERSIZED_BODY_BYTES)
        except OSError:
            return

    @override
    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _payload(dashboard: AdminDashboardRead) -> bytes:
    return dashboard.model_dump_json().encode("utf-8")


def _replace(payload: bytes, old: bytes, new: bytes) -> bytes:
    assert old in payload
    return payload.replace(old, new, 1)


def _dashboard(
    *,
    runtime: RuntimeState = RuntimeState.OPERATIONAL,
    readiness: ReadinessCause = ReadinessCause.READY,
    ledger_status: LedgerStatus = LedgerStatus.OK,
    blocker: CapacityBlocker = CapacityBlocker.NONE,
) -> AdminDashboardRead:
    base = FakeAdminState().dashboard()
    overview = base.overview
    upstreams = base.upstream_keys
    if readiness is ReadinessCause.READY:
        first = upstreams.items[0].model_copy(
            update={
                "enabled": True,
                "routing_state": UpstreamRoutingState.ELIGIBLE,
                "health_state": HealthState.HEALTHY,
            }
        )
        upstreams = upstreams.model_copy(update={"items": (first, *upstreams.items[1:])})
        overview = overview.model_copy(
            update={
                "status": OverviewStatus.OK,
                "ready": True,
                "upstream_keys": overview.upstream_keys.model_copy(
                    update={"enabled": 2, "eligible": 1}
                ),
            }
        )
    ledger_update: dict[str, object] = {
        "status": ledger_status,
        "capacity_blocker": blocker,
    }
    if ledger_status is LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING:
        ledger_update["event_rows"] = base.ledger.event_capacity
    return base.model_copy(
        update={
            "runtime_state": runtime,
            "readiness_cause": readiness,
            "ledger": base.ledger.model_copy(update=ledger_update),
            "overview": overview,
            "upstream_keys": upstreams,
        }
    )


def _readiness(
    *,
    runtime: RuntimeState = RuntimeState.OPERATIONAL,
    cause: ReadinessCause = ReadinessCause.READY,
    ledger_status: LedgerStatus = LedgerStatus.OK,
    blocker: CapacityBlocker = CapacityBlocker.NONE,
) -> bytes:
    return (
        AdminOperatorReadinessRead(
            runtime_state=runtime,
            readiness_cause=cause,
            ledger_status=ledger_status,
            capacity_blocker=blocker,
        )
        .model_dump_json()
        .encode("utf-8")
    )


def test_bounded_operator_readiness_probe_accepts_only_coherent_mode_states() -> None:
    runtime_states = (
        _readiness(),
        _readiness(cause=ReadinessCause.NO_ELIGIBLE_UPSTREAM),
        _readiness(
            cause=ReadinessCause.LEDGER_CAPACITY_EXHAUSTED,
            ledger_status=LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING,
            blocker=CapacityBlocker.ACTIVE_ATTEMPTS,
        ),
        _readiness(
            cause=ReadinessCause.LEDGER_CAPACITY_EXHAUSTED,
            ledger_status=LedgerStatus.CAPACITY_BLOCKED,
            blocker=CapacityBlocker.ORPHANED_PENDING,
        ),
    )
    assert all(
        operator_readiness_probe_confirmed(200, payload, OperatorProbeMode.RUNTIME)
        for payload in runtime_states
    )
    assert all(
        operator_readiness_probe_confirmed(200, payload, OperatorProbeMode.LEDGER_CAPACITY)
        for payload in (
            _readiness(),
            _readiness(cause=ReadinessCause.NO_ELIGIBLE_UPSTREAM),
            _readiness(ledger_status=LedgerStatus.MAINTENANCE_OVERDUE),
        )
    )

    invalid = (
        _readiness(
            runtime=RuntimeState.UNAVAILABLE,
            cause=ReadinessCause.RUNTIME_UNAVAILABLE,
        ),
        _readiness(cause=ReadinessCause.RUNTIME_UNAVAILABLE),
        _readiness(cause=ReadinessCause.LEDGER_CAPACITY_EXHAUSTED),
        _readiness(
            ledger_status=LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING,
            blocker=CapacityBlocker.ACTIVE_ATTEMPTS,
        ),
        _readiness(blocker=CapacityBlocker.ACTIVE_ATTEMPTS),
        b'{"runtime_state":"operational","readiness_cause":"ready"}',
        _readiness()[:-1] + b',"unexpected":true}',
    )
    assert all(
        not operator_readiness_probe_confirmed(200, payload, mode)
        for mode in OperatorProbeMode
        for payload in invalid
    )
    assert not operator_readiness_probe_confirmed(
        503,
        _readiness(),
        OperatorProbeMode.RUNTIME,
    )


def test_runtime_probe_accepts_every_operational_routing_and_ledger_state() -> None:
    operational_states = (
        _dashboard(),
        _dashboard(readiness=ReadinessCause.NO_ELIGIBLE_UPSTREAM),
        _dashboard(
            readiness=ReadinessCause.LEDGER_CAPACITY_EXHAUSTED,
            ledger_status=LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING,
            blocker=CapacityBlocker.ACTIVE_ATTEMPTS,
        ),
        _dashboard(
            readiness=ReadinessCause.LEDGER_CAPACITY_EXHAUSTED,
            ledger_status=LedgerStatus.CAPACITY_BLOCKED,
            blocker=CapacityBlocker.ORPHANED_PENDING,
        ),
    )

    assert all(
        dashboard_probe_confirmed(200, _payload(item), OperatorProbeMode.RUNTIME)
        for item in operational_states
    )
    assert not dashboard_probe_confirmed(
        200,
        _payload(
            _dashboard(
                runtime=RuntimeState.UNAVAILABLE,
                readiness=ReadinessCause.RUNTIME_UNAVAILABLE,
            )
        ),
        OperatorProbeMode.RUNTIME,
    )
    assert not dashboard_probe_confirmed(
        200,
        _payload(_dashboard(readiness=ReadinessCause.RUNTIME_UNAVAILABLE)),
        OperatorProbeMode.RUNTIME,
    )
    for status, payload in ((503, _payload(_dashboard())), (200, b"{"), (200, b"{}")):
        assert not dashboard_probe_confirmed(status, payload, OperatorProbeMode.RUNTIME)


def test_dashboard_probe_rejects_partial_non_dto_or_incoherent_surface() -> None:
    valid = _payload(_dashboard())
    invalid = (
        b'{"runtime_state":"operational","readiness_cause":"ready"}',
        valid[:-1] + b',"unexpected":true}',
        _replace(valid, b'"status":"ok"', b'"status":"future"'),
        _replace(valid, b'"upstream_keys":{"total":2', b'"upstream_keys":{"total":"2"'),
        _replace(valid, b'"routing_state":"eligible"', b'"routing_state":"future"'),
        _replace(
            valid,
            b'"scopes":["models:read","chat:write"]',
            b'"scopes":[]',
        ),
        _replace(
            valid,
            b'"attempt_started_event_id":null',
            b'"unexpected_event_field":null',
        ),
        _replace(
            valid,
            b'"status":"ok","ready":true',
            b'"status":"degraded","ready":false',
        ),
        _replace(valid, b'"event_rows":1', b'"event_rows":10000'),
        _replace(valid, b'"upstream_keys":{"total":2', b'"upstream_keys":{"total":3'),
        _replace(
            valid,
            b'"generated_at":"2026-01-01T00:00:00Z"',
            b'"generated_at":"2026-01-01T00:00:00+00:00"',
        ),
    )

    assert all(
        not dashboard_probe_confirmed(200, payload, mode)
        for mode in OperatorProbeMode
        for payload in invalid
    )


def test_dashboard_probe_orders_parsed_utc_instants_before_uuid_ties() -> None:
    def payload(first_at: str, first_id: str, second_at: str, second_id: str) -> bytes:
        dashboard = _dashboard()
        template = dashboard.events.items[0]
        first_timestamp = datetime.fromisoformat(first_at)
        second_timestamp = datetime.fromisoformat(second_at)
        events = dashboard.events.model_copy(
            update={
                "items": (
                    template.model_copy(
                        update={"id": UUID(first_id), "occurred_at": first_timestamp}
                    ),
                    template.model_copy(
                        update={"id": UUID(second_id), "occurred_at": second_timestamp}
                    ),
                )
            }
        )
        return _payload(
            dashboard.model_copy(
                update={
                    "events": events,
                    "overview": dashboard.overview.model_copy(
                        update={"last_event_at": first_timestamp}
                    ),
                    "ledger": dashboard.ledger.model_copy(
                        update={"event_rows": 2, "oldest_event_at": second_timestamp}
                    ),
                }
            )
        )

    low_id = "00000000-0000-4000-8000-000000000001"
    high_id = "00000000-0000-4000-8000-000000000002"
    whole = "2026-07-12T01:02:03Z"
    fraction = "2026-07-12T01:02:03.100000Z"
    later_microsecond = "2026-07-12T01:02:03.100001Z"

    accepted = (
        payload(fraction, low_id, whole, high_id),
        payload(later_microsecond, low_id, fraction, high_id),
        payload(whole, high_id, whole, low_id),
    )
    rejected = (
        payload(whole, high_id, fraction, low_id),
        payload(fraction, high_id, later_microsecond, low_id),
        payload(whole, low_id, whole, high_id),
    )
    assert all(dashboard_probe_confirmed(200, item, OperatorProbeMode.RUNTIME) for item in accepted)
    assert all(
        not dashboard_probe_confirmed(200, item, OperatorProbeMode.RUNTIME) for item in rejected
    )


def test_capacity_probe_accepts_only_nonblocked_ready_or_no_key_state() -> None:
    for readiness in (ReadinessCause.READY, ReadinessCause.NO_ELIGIBLE_UPSTREAM):
        for ledger_status in (LedgerStatus.OK, LedgerStatus.MAINTENANCE_OVERDUE):
            assert dashboard_probe_confirmed(
                200,
                _payload(_dashboard(readiness=readiness, ledger_status=ledger_status)),
                OperatorProbeMode.LEDGER_CAPACITY,
            )

    rejected = [
        _dashboard(
            runtime=RuntimeState.UNAVAILABLE,
            readiness=ReadinessCause.RUNTIME_UNAVAILABLE,
        ),
        _dashboard(readiness=ReadinessCause.RUNTIME_UNAVAILABLE),
        _dashboard(readiness=ReadinessCause.LEDGER_CAPACITY_EXHAUSTED),
        _dashboard(ledger_status=LedgerStatus.CAPACITY_EXHAUSTED_RECOVERING),
        _dashboard(ledger_status=LedgerStatus.CAPACITY_BLOCKED),
    ]
    rejected.extend(
        _dashboard(blocker=blocker)
        for blocker in CapacityBlocker
        if blocker is not CapacityBlocker.NONE
    )
    assert all(
        not dashboard_probe_confirmed(
            200,
            _payload(item),
            OperatorProbeMode.LEDGER_CAPACITY,
        )
        for item in rejected
    )
    for status, payload in ((503, _payload(_dashboard())), (200, b"{"), (200, b"{}")):
        assert not dashboard_probe_confirmed(
            status,
            payload,
            OperatorProbeMode.LEDGER_CAPACITY,
        )


def test_legacy_overview_fallback_accepts_ready_and_degraded_operational_reads() -> None:
    ready = _dashboard().overview.model_dump_json().encode("utf-8")
    degraded = (
        _dashboard(readiness=ReadinessCause.NO_ELIGIBLE_UPSTREAM)
        .overview.model_dump_json()
        .encode("utf-8")
    )

    assert legacy_overview_confirmed(200, ready)
    assert legacy_overview_confirmed(200, degraded)
    assert not legacy_overview_confirmed(503, degraded)
    assert not legacy_overview_confirmed(200, b"{")
    assert not legacy_overview_confirmed(200, b"{}")
    malformed = b"".join(
        (
            b'{"status":"ok","ready":true,"upstream_keys":{},',
            b'"downstream_tokens":{},"request_count":1,"last_event_at":[],',
            b'"generated_at":"not-a-timestamp"}',
        )
    )
    assert not legacy_overview_confirmed(200, malformed)
    assert not legacy_overview_confirmed(
        200,
        _replace(ready, b'"eligible":1', b'"eligible":0'),
    )
    assert not legacy_overview_confirmed(
        200,
        _replace(
            ready,
            b'"generated_at":"2026-01-01T00:00:00Z"',
            b'"generated_at":"2026-01-01T00:00:00+00:00"',
        ),
    )


def test_cli_separates_invalid_arguments_from_token_or_runtime_failure(tmp_path: Path) -> None:
    missing_token = tmp_path / "missing-token"
    cases = (
        (("unknown", "2456", missing_token), 64),
        (("runtime", "not-a-port", missing_token), 64),
        (("runtime", "0", missing_token), 64),
        (("runtime", "65536", missing_token), 64),
        (("runtime", "02456", missing_token), 64),
        (("runtime", "+2456", missing_token), 64),
        (("runtime", " 2456", missing_token), 64),
        (("runtime", "2456 ", missing_token), 64),
        (("runtime", "2_456", missing_token), 64),
        (("runtime", "\uff12\uff14\uff15\uff16", missing_token), 64),
        (("runtime", "2456", missing_token, "not-a-container"), 64),
        (("ledger-capacity", "2456", missing_token, "a" * 64), 64),
        (("runtime", "1", missing_token), 1),
        (("runtime", "2456", missing_token), 1),
        (("runtime", "65535", missing_token), 1),
    )

    for arguments, expected in cases:
        completed = subprocess.run(  # noqa: S603 - fixed host probe under test.
            [sys.executable, _SCRIPT, *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == expected
        assert completed.stdout == ""
        assert completed.stderr == ""


def _run_host_probe(
    tmp_path: Path,
    *,
    readiness_payload: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    _LegacyOverviewHandler.readiness_payload = readiness_payload
    _LegacyOverviewHandler.overview_payload = (
        _dashboard().overview.model_dump_json().encode("utf-8")
    )
    _LegacyOverviewHandler.observed_paths = []
    _LegacyOverviewHandler.observed_hosts = []
    _LegacyOverviewHandler.overview_calls = 0
    token_file = tmp_path / "admin_token"
    _ = token_file.write_text(_TOKEN, encoding="ascii")
    token_file.chmod(0o600)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LegacyOverviewHandler)
    assert server.server_port != 2456
    expected_host = f"127.0.0.1:{server.server_port}"
    _LegacyOverviewHandler.expected_host = expected_host
    thread = Thread(target=server.serve_forever, daemon=False)
    thread.start()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed host probe under test.
            [
                _ISOLATED_PYTHON,
                "-I",
                _SCRIPT,
                "runtime",
                str(server.server_port),
                token_file,
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return completed, expected_host


def test_host_probe_executes_stable_legacy_fallback_without_target_module(
    tmp_path: Path,
) -> None:
    assert _ISOLATED_PYTHON.is_file()
    completed, expected_host = _run_host_probe(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert _LegacyOverviewHandler.observed_paths == [
        "/admin/api/v1/operator-readiness",
        "/admin/api/v1/overview",
    ]
    assert _LegacyOverviewHandler.observed_hosts == [expected_host] * 2


def test_host_probe_uses_only_bounded_current_endpoint(
    tmp_path: Path,
) -> None:
    completed, expected_host = _run_host_probe(tmp_path, readiness_payload=_readiness())

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert _LegacyOverviewHandler.observed_paths == ["/admin/api/v1/operator-readiness"]
    assert _LegacyOverviewHandler.observed_hosts == [expected_host]


@pytest.mark.parametrize(("repeated_status", "expected"), [(200, True), (503, False)])
def test_legacy_degraded_fallback_outlives_fail_stop_budget(
    monkeypatch: pytest.MonkeyPatch,
    repeated_status: int,
    expected: bool,
) -> None:
    degraded = (
        _dashboard(readiness=ReadinessCause.NO_ELIGIBLE_UPSTREAM)
        .overview.model_dump_json()
        .encode("utf-8")
    )
    observed_sleeps: list[float] = []
    observed_requests: list[tuple[int, str]] = []
    container_id = "a" * 64
    generation = (container_id, "2026-07-15T01:02:03.123456789Z")

    def fake_sleep(seconds: float) -> None:
        observed_sleeps.append(seconds)

    def fake_request(port: int, token: str, path: str) -> tuple[int, bytes]:
        del token
        observed_requests.append((port, path))
        return repeated_status, degraded if repeated_status == 200 else b"{}"

    def stable_generation(observed: str) -> tuple[str, str] | None:
        return generation if observed == container_id else None

    monkeypatch.setattr("scripts.operator_readiness_probe.sleep", fake_sleep)
    monkeypatch.setattr(
        "scripts.operator_readiness_probe.operator_http_request",
        fake_request,
    )
    monkeypatch.setattr(
        "scripts.operator_readiness_probe.container_generation",
        stable_generation,
    )

    assert legacy_runtime_confirmed(32458, _TOKEN, 200, degraded, container_id) is expected
    assert observed_sleeps == [30.0]
    assert observed_requests == [(32458, "/admin/api/v1/overview")]


def test_legacy_degraded_fallback_rejects_missing_or_changed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    degraded = (
        _dashboard(readiness=ReadinessCause.NO_ELIGIBLE_UPSTREAM)
        .overview.model_dump_json()
        .encode("utf-8")
    )
    container_id = "a" * 64
    generations = iter(
        (
            (container_id, "2026-07-15T01:02:03.123456789Z"),
            (container_id, "2026-07-15T01:02:34.123456789Z"),
        )
    )

    def no_sleep(seconds: float) -> None:
        del seconds

    def repeated_request(port: int, token: str, path: str) -> tuple[int, bytes]:
        del port, token, path
        return 200, degraded

    def changed_generation(observed: str) -> tuple[str, str]:
        assert observed == container_id
        return next(generations)

    monkeypatch.setattr("scripts.operator_readiness_probe.sleep", no_sleep)
    monkeypatch.setattr(
        "scripts.operator_readiness_probe.operator_http_request",
        repeated_request,
    )
    monkeypatch.setattr(
        "scripts.operator_readiness_probe.container_generation",
        changed_generation,
    )

    assert not legacy_runtime_confirmed(32458, _TOKEN, 200, degraded)
    assert not legacy_runtime_confirmed(32458, _TOKEN, 200, degraded, container_id)


def test_container_generation_uses_only_filtered_bounded_docker_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container_id = "a" * 64
    observed: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        arguments: list[str],
        **options: object,
    ) -> subprocess.CompletedProcess[str]:
        observed.append((arguments, options))
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=f"{container_id}\t2026-07-15T01:02:03.123456789Z\n",
            stderr=None,
        )

    monkeypatch.setattr("scripts.operator_readiness_probe.subprocess.run", fake_run)

    assert container_generation(container_id) == (
        container_id,
        "2026-07-15T01:02:03.123456789Z",
    )
    assert observed[0][0] == [
        "/usr/bin/docker",
        "inspect",
        "--format",
        "{{.Id}}\t{{.State.StartedAt}}",
        container_id,
    ]
    assert observed[0][1]["env"] == {"PATH": "/usr/bin:/bin"}
    assert observed[0][1]["timeout"] == 2.0


def test_host_request_has_absolute_deadline_against_slow_body() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowBodyHandler)
    _SlowBodyHandler.expected_host = f"127.0.0.1:{server.server_port}"
    thread = Thread(target=server.serve_forever, daemon=False)
    thread.start()
    started = monotonic()
    try:
        with pytest.raises(TimeoutError):
            _ = operator_http_request(
                server.server_port,
                _TOKEN,
                "/admin/api/v1/operator-readiness",
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    elapsed = monotonic() - started
    assert 1.5 <= elapsed < 3.0


def test_host_request_rejects_oversized_streamed_body() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OversizedBodyHandler)
    thread = Thread(target=server.serve_forever, daemon=False)
    thread.start()
    try:
        with pytest.raises(ValueError, match="response too large"):
            _ = operator_http_request(
                server.server_port,
                _TOKEN,
                "/admin/api/v1/operator-readiness",
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
