import http.client
import json
from pathlib import Path
from types import TracebackType
from typing import Self, final

from nvidia_build_lb.admin.schemas import (
    DownstreamTokenListResponse,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)

_AUTHORITY = "127.0.0.1:2456"
_API_ROOT = "/admin/api/v1"
_MAX_RESPONSE_BYTES = 1_048_576


class BrowserProdClientError(RuntimeError):
    pass


@final
class BrowserProdAdminClient:
    def __init__(self, admin_token_file: Path) -> None:
        token = admin_token_file.read_text(encoding="ascii")
        if not token.startswith("nblb_admin_") or len(token) != 75:
            reason = "invalid browser QA admin token file"
            raise BrowserProdClientError(reason)
        self.admin_bearer: str = token
        self.downstream_bearer: str = "nblb_ds_" + ("0" * 64)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback

    def _request(
        self,
        method: str,
        path: str,
        expected_status: int,
        body: dict[str, object] | None = None,
    ) -> bytes:
        connection = http.client.HTTPConnection("127.0.0.1", 2456, timeout=5)
        payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.admin_bearer}",
            "Host": _AUTHORITY,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, f"{_API_ROOT}{path}", body=payload, headers=headers)
            response = connection.getresponse()
            content = response.read(_MAX_RESPONSE_BYTES + 1)
        finally:
            connection.close()
        if len(content) > _MAX_RESPONSE_BYTES:
            reason = "browser QA admin response exceeded bound"
            raise BrowserProdClientError(reason)
        if response.status != expected_status:
            reason = f"browser QA admin request returned status {response.status}"
            raise BrowserProdClientError(reason)
        return content

    def upstreams(self) -> UpstreamKeyListResponse:
        return UpstreamKeyListResponse.model_validate_json(
            self._request("GET", "/upstream-keys", 200)
        )

    def tokens(self) -> DownstreamTokenListResponse:
        return DownstreamTokenListResponse.model_validate_json(
            self._request("GET", "/downstream-tokens", 200)
        )

    def create_upstream(self, key: str) -> UpstreamKeyRead:
        return UpstreamKeyRead.model_validate_json(
            self._request("POST", "/upstream-keys", 201, {"key": key})
        )

    def probe_upstream(self, item_id: str) -> UpstreamProbeResponse:
        return UpstreamProbeResponse.model_validate_json(
            self._request("POST", f"/upstream-keys/{item_id}/probe", 200)
        )

    def enable_upstream(self, item_id: str) -> None:
        _ = self._request("POST", f"/upstream-keys/{item_id}/enable", 204)

    def disable_upstream(self, item_id: str) -> None:
        _ = self._request("POST", f"/upstream-keys/{item_id}/disable", 204)

    def delete_upstream(self, item_id: str) -> None:
        _ = self._request("DELETE", f"/upstream-keys/{item_id}", 204)

    def revoke_token(self, item_id: str) -> None:
        _ = self._request("DELETE", f"/downstream-tokens/{item_id}", 204)

    def seed_ready_key(self, run_name: str) -> UpstreamKeyRead:
        created = self.create_upstream(f"nvapi-synthetic-browser-prod-ready-{run_name}")
        probe = self.probe_upstream(str(created.id))
        if probe.probe_status != "valid":
            reason = "synthetic NVIDIA probe did not become valid"
            raise BrowserProdClientError(reason)
        self.enable_upstream(str(created.id))
        enabled = next(item for item in self.upstreams().items if item.id == created.id)
        if not enabled.enabled:
            reason = "synthetic NVIDIA key was not enabled"
            raise BrowserProdClientError(reason)
        return enabled

    def cleanup_rows(self) -> None:
        for item in self.tokens().items:
            if item.revoked_at is None:
                self.revoke_token(str(item.id))
        for item in self.upstreams().items:
            if item.enabled:
                self.disable_upstream(str(item.id))
        for item in self.upstreams().items:
            self.delete_upstream(str(item.id))
