from hmac import compare_digest
from typing import Final, final, override
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from orjson import dumps
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from nvidia_build_lb.admin.schemas import (
    AdminDashboardRead,
    AdminEventListResponse,
    AdminOverviewRead,
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)
from nvidia_build_lb.web.admin_resources import create_admin_resource_router

from .fake_admin_state import FakeAdminError, FakeAdminState, SafeNetworkObservation

_AUTHORITY: Final = "127.0.0.1:2456"
_ORIGIN: Final = "http://127.0.0.1:2456"
_CSP: Final = (
    "default-src 'none'; base-uri 'none'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; img-src 'self'; object-src 'none'; script-src 'self'; "
    "style-src 'self'"
)
_SECURITY_HEADERS: Final = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": _CSP,
}


def _safe_error(status_code: int, code: str, message: str) -> Response:
    return Response(
        status_code=status_code,
        content=dumps({"error": {"code": code, "message": message, "request_id": "fake-request"}}),
        media_type="application/json",
        headers=_SECURITY_HEADERS,
    )


@final
class _BoundaryMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, state_store: FakeAdminState) -> None:
        super().__init__(app)
        self._state_store = state_store

    def _observed(self, request: Request, response: Response) -> Response:
        self._state_store.record_network(
            SafeNetworkObservation(
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                query_present=bool(request.url.query),
            )
        )
        return response

    @override
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        state_store = self._state_store
        state_store.record_boundary("host")
        host_values = request.headers.getlist("host")
        if host_values != [_AUTHORITY]:
            return self._observed(
                request,
                _safe_error(
                    status.HTTP_403_FORBIDDEN,
                    "host_forbidden",
                    "request authority is forbidden",
                ),
            )
        state_store.record_boundary("origin")
        origin_values = request.headers.getlist("origin")
        if origin_values and origin_values != [_ORIGIN]:
            return self._observed(
                request,
                _safe_error(
                    status.HTTP_403_FORBIDDEN,
                    "origin_forbidden",
                    "request origin is forbidden",
                ),
            )
        if request.method == "OPTIONS":
            state_store.record_boundary("options_405")
            return self._observed(
                request,
                _safe_error(
                    status.HTTP_405_METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "method is not allowed",
                ),
            )
        if request.url.path.startswith("/admin/api/"):
            state_store.record_boundary("auth")
            authorization_values = request.headers.getlist("authorization")
            expected = f"Bearer {state_store.admin_bearer}"
            authenticated = len(authorization_values) == 1 and compare_digest(
                authorization_values[0],
                expected,
            )
            if not authenticated:
                response = _safe_error(
                    status.HTTP_401_UNAUTHORIZED,
                    "admin_unauthorized",
                    "admin authentication required",
                )
                response.headers["WWW-Authenticate"] = 'Bearer realm="nvidia-build-lb-admin"'
                return self._observed(request, response)
            if request.url.query:
                return self._observed(
                    request,
                    _safe_error(
                        status.HTTP_422_UNPROCESSABLE_CONTENT,
                        "invalid_request",
                        "request validation failed",
                    ),
                )
        else:
            state_store.record_boundary("no_op")
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        return self._observed(request, response)


@final
class _FakeAdminController:
    def __init__(self, state_store: FakeAdminState) -> None:
        self._state_store = state_store

    async def dashboard(self) -> AdminDashboardRead:
        self._state_store.record_boundary("route:dashboard")
        self._state_store.check_available("/admin/api/v1/dashboard")
        return self._state_store.dashboard()

    async def overview(self) -> AdminOverviewRead:
        self._state_store.record_boundary("route:overview")
        self._state_store.check_available("/admin/api/v1/overview")
        return self._state_store.overview()

    async def upstream_list(self) -> UpstreamKeyListResponse:
        self._state_store.record_boundary("route:upstream_list")
        self._state_store.check_available("/admin/api/v1/upstream-keys")
        return self._state_store.upstreams()

    async def upstream_create(self, request: UpstreamKeyCreateRequest) -> UpstreamKeyRead:
        self._state_store.record_boundary("route:upstream_create")
        self._state_store.check_available("/admin/api/v1/upstream-keys")
        return self._state_store.add_upstream(request)

    async def upstream_enable(self, item_id: UUID) -> Response:
        self._state_store.record_boundary("route:upstream_enable")
        _ = self._state_store.change_upstream(item_id, "enable")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def upstream_disable(self, item_id: UUID) -> Response:
        self._state_store.record_boundary("route:upstream_disable")
        _ = self._state_store.change_upstream(item_id, "disable")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def upstream_probe(self, item_id: UUID) -> UpstreamProbeResponse:
        self._state_store.record_boundary("route:upstream_probe")
        result = self._state_store.change_upstream(item_id, "probe")
        if result is None:
            raise FakeAdminError(500, "fake_contract_error", "fake probe result unavailable")
        return result

    async def upstream_delete(self, item_id: UUID) -> Response:
        self._state_store.record_boundary("route:upstream_delete")
        _ = self._state_store.change_upstream(item_id, "delete")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def downstream_list(self) -> DownstreamTokenListResponse:
        self._state_store.record_boundary("route:downstream_list")
        self._state_store.check_available("/admin/api/v1/downstream-tokens")
        return self._state_store.tokens()

    async def downstream_issue(
        self,
        request: DownstreamTokenIssueRequest,
    ) -> DownstreamTokenIssued:
        self._state_store.record_boundary("route:downstream_issue")
        self._state_store.check_available("/admin/api/v1/downstream-tokens")
        return self._state_store.issue_token(request)

    async def downstream_revoke(self, item_id: UUID) -> Response:
        self._state_store.record_boundary("route:downstream_revoke")
        self._state_store.revoke_token(item_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    async def events(self) -> AdminEventListResponse:
        self._state_store.record_boundary("route:events")
        self._state_store.check_available("/admin/api/v1/events")
        return self._state_store.events()


def _create_api_router(controller: _FakeAdminController) -> APIRouter:
    router = APIRouter(prefix="/admin/api/v1")
    router.add_api_route("/dashboard", controller.dashboard, methods=["GET"])
    router.add_api_route("/overview", controller.overview, methods=["GET"])
    router.add_api_route("/upstream-keys", controller.upstream_list, methods=["GET"])
    router.add_api_route(
        "/upstream-keys",
        controller.upstream_create,
        methods=["POST"],
        status_code=status.HTTP_201_CREATED,
    )
    router.add_api_route(
        "/upstream-keys/{item_id}/enable",
        controller.upstream_enable,
        methods=["POST"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    router.add_api_route(
        "/upstream-keys/{item_id}/disable",
        controller.upstream_disable,
        methods=["POST"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    router.add_api_route(
        "/upstream-keys/{item_id}/probe",
        controller.upstream_probe,
        methods=["POST"],
    )
    router.add_api_route(
        "/upstream-keys/{item_id}",
        controller.upstream_delete,
        methods=["DELETE"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    router.add_api_route("/downstream-tokens", controller.downstream_list, methods=["GET"])
    router.add_api_route(
        "/downstream-tokens",
        controller.downstream_issue,
        methods=["POST"],
        status_code=status.HTTP_201_CREATED,
    )
    router.add_api_route(
        "/downstream-tokens/{item_id}",
        controller.downstream_revoke,
        methods=["DELETE"],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    router.add_api_route("/events", controller.events, methods=["GET"])
    return router


def create_fake_admin_app(state_store: FakeAdminState) -> FastAPI:
    """Compose fixed UI resources with exact fake administration DTO routes."""
    application = FastAPI(
        title="NVIDIA Build LB UI fake",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.include_router(create_admin_resource_router())
    application.include_router(_create_api_router(_FakeAdminController(state_store)))
    application.add_middleware(_BoundaryMiddleware, state_store=state_store)

    async def _fake_error_handler(request: Request, error: FakeAdminError) -> Response:
        del request
        return _safe_error(error.status_code, error.code, error.message)

    async def _validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> Response:
        del request, error
        return _safe_error(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "invalid_request",
            "request validation failed",
        )

    _ = application.exception_handler(FakeAdminError)(_fake_error_handler)
    _ = application.exception_handler(RequestValidationError)(_validation_error_handler)
    return application
