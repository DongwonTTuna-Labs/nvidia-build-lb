from typing import Protocol, final
from urllib.parse import urlsplit

from playwright.sync_api import Page

from .browser_observability_contract import network_observation_is_allowed
from .browser_prod_models import BrowserNetworkProjection
from .fake_admin_state import SafeNetworkObservation

_AUTHORITY = "127.0.0.1:2456"


class _RequestLike(Protocol):
    @property
    def method(self) -> str: ...

    @property
    def url(self) -> str: ...


class _ResponseLike(Protocol):
    @property
    def request(self) -> _RequestLike: ...

    @property
    def status(self) -> int: ...

    @property
    def url(self) -> str: ...


@final
class ProductionNetworkAudit:
    def __init__(self) -> None:
        self._phase: str = "unclassified"
        self._items: list[tuple[_RequestLike, BrowserNetworkProjection]] = []
        self._response_requests: list[_RequestLike] = []

    def set_phase(self, phase: str) -> None:
        self._phase = phase

    def attach(self, page: Page) -> None:
        page.on("response", self.observe_response)
        page.on("requestfailed", self.observe_failed)

    def detach(self, page: Page) -> None:
        page.remove_listener("response", self.observe_response)
        page.remove_listener("requestfailed", self.observe_failed)

    def _safe_path(self, url: str) -> tuple[str, bool]:
        parsed = urlsplit(url)
        if parsed.netloc != _AUTHORITY or parsed.scheme != "http":
            reason = "browser request left the fixed loopback authority"
            raise AssertionError(reason)
        return parsed.path, bool(parsed.query)

    def observe_response(self, response: _ResponseLike) -> None:
        path, query_present = self._safe_path(response.url)
        request = response.request
        self._response_requests.append(request)
        self._items.append(
            (
                request,
                BrowserNetworkProjection(
                    phase=self._phase,
                    method=response.request.method,
                    path=path,
                    status=response.status,
                    failed=False,
                    query_present=query_present,
                ),
            )
        )

    def observe_failed(self, request: _RequestLike) -> None:
        path, query_present = self._safe_path(request.url)
        self._items.append(
            (
                request,
                BrowserNetworkProjection(
                    phase=self._phase,
                    method=request.method,
                    path=path,
                    status=None,
                    failed=True,
                    query_present=query_present,
                ),
            )
        )

    def verified(self) -> tuple[BrowserNetworkProjection, ...]:
        items = tuple(
            item
            for request, item in self._items
            if not item.failed
            or not any(request is response_request for response_request in self._response_requests)
        )
        if any(item.query_present for item in items):
            reason = "browser QA observed a query-bearing request"
            raise AssertionError(reason)
        failed = [item for item in items if item.failed]
        expected_failures = {
            ("offline_refresh", "GET", "/admin/api/v1/overview"),
            ("native-offline_refresh", "GET", "/admin/api/v1/overview"),
        }
        observed_failures = {(item.phase, item.method, item.path) for item in failed}
        if observed_failures != expected_failures or len(failed) != len(expected_failures):
            projection = tuple(sorted((item.phase, item.method, item.path) for item in failed))
            reason = f"browser QA failed-request projection changed: {projection}"
            raise AssertionError(reason)
        for item in items:
            if item.failed:
                continue
            observation = SafeNetworkObservation(
                method=item.method,
                path=item.path,
                status=item.status or 0,
                query_present=item.query_present,
            )
            if not network_observation_is_allowed(observation):
                reason = "browser QA observed a disallowed method/path/status"
                raise AssertionError(reason)
        return items
