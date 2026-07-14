"""Production admin probe maps only durable routed outcomes."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest

from nvidia_build_lb.admin.schemas import ProbeStatus, UpstreamProbeResponse
from nvidia_build_lb.outcomes import HttpStatusSignal, map_public_outcome
from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.production_probe import RoutingProbeExecutor
from nvidia_build_lb.routing import RoutedFailure
from tests.contracts.fakes import FakeRouter

pytestmark = pytest.mark.anyio

_KEY_ID = UUID("00000000-0000-4000-8000-000000000001")


@dataclass(slots=True)
class _Upstream:
    observed: list[ProbeStatus]

    async def probe_result(self, key_id: UUID, status: ProbeStatus) -> UpstreamProbeResponse:
        assert key_id == _KEY_ID
        self.observed.append(status)
        return UpstreamProbeResponse(
            id=key_id,
            enabled=False,
            probe_status=status,
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


@dataclass(frozen=True, slots=True)
class _FailureRouter:
    status_code: int

    async def execute(
        self,
        *,
        request_id: str,
        body: bytes,
        requested_stream: bool = False,
        explicit_probe_key_id: UUID | None = None,
    ) -> RoutedFailure:
        del request_id, body, requested_stream
        assert explicit_probe_key_id == _KEY_ID
        return RoutedFailure(
            FailureTerminal(map_public_outcome(HttpStatusSignal(self.status_code)), None),
            1,
        )


async def test_probe_success_is_projected_as_valid() -> None:
    upstream = _Upstream([])

    result = await RoutingProbeExecutor(FakeRouter(), upstream).probe(_KEY_ID, "probe-request")

    assert result.probe_status is ProbeStatus.VALID
    assert upstream.observed == [ProbeStatus.VALID]


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, ProbeStatus.INVALID_CREDENTIAL),
        (429, ProbeStatus.RATE_LIMITED),
        (503, ProbeStatus.UPSTREAM_UNAVAILABLE),
    ],
)
async def test_probe_failure_uses_closed_safe_status(
    status_code: int,
    expected: ProbeStatus,
) -> None:
    upstream = _Upstream([])

    result = await RoutingProbeExecutor(_FailureRouter(status_code), upstream).probe(
        _KEY_ID,
        "probe-request",
    )

    assert result.probe_status is expected
    assert upstream.observed == [expected]
