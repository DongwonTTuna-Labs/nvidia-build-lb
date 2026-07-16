import gc
import weakref
from dataclasses import dataclass

from .browser_prod_observability import ProductionNetworkAudit

_UUID = "12345678-1234-1234-1234-123456789abc"


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    url: str


@dataclass(frozen=True, slots=True)
class _Response:
    request: _Request
    status: int

    @property
    def url(self) -> str:
        return self.request.url


@dataclass(frozen=True)
class _WeakRequest:
    method: str
    url: str


@dataclass(frozen=True, slots=True)
class _WeakResponse:
    request: _WeakRequest
    status: int

    @property
    def url(self) -> str:
        return self.request.url


def test_answered_request_identity_is_retained_until_projection_verification() -> None:
    audit = ProductionNetworkAudit()
    answered = _WeakRequest(method="GET", url="http://127.0.0.1:2456/health")
    answered_reference = weakref.ref(answered)

    audit.observe_response(_WeakResponse(request=answered, status=200))
    del answered
    _ = gc.collect()

    assert answered_reference() is not None


def test_answered_request_failure_is_removed_from_verified_projection() -> None:
    audit = ProductionNetworkAudit()
    answered = _Request(
        method="DELETE",
        url=f"http://127.0.0.1:2456/admin/api/v1/upstream-keys/{_UUID}",
    )
    audit.set_phase("upstream_delete")
    audit.observe_failed(answered)
    audit.observe_response(_Response(request=answered, status=204))

    audit.set_phase("auth_initial_offline")
    audit.observe_failed(_Request(method="GET", url="http://127.0.0.1:2456/admin/api/v1/dashboard"))
    audit.set_phase("offline_refresh")
    audit.observe_failed(_Request(method="GET", url="http://127.0.0.1:2456/admin/api/v1/dashboard"))
    audit.set_phase("native-offline_refresh")
    audit.observe_failed(_Request(method="GET", url="http://127.0.0.1:2456/admin/api/v1/dashboard"))
    for phase, action, status in (
        ("upstream_probe_503", "probe", 503),
        ("upstream_enable_401", "enable", 401),
        ("native-upstream_probe_503", "probe", 503),
        ("native-upstream_enable_401", "enable", 401),
    ):
        audit.set_phase(phase)
        request = _Request(
            method="POST",
            url=f"http://127.0.0.1:2456/admin/api/v1/upstream-keys/{_UUID}/{action}",
        )
        audit.observe_response(_Response(request=request, status=status))

    projection = audit.verified()

    assert len(projection) == 8
    assert sum(item.failed for item in projection) == 3
    assert any(item.status == 204 and not item.failed for item in projection)
