"""Secret-free admin reads and closed unknown-resource fallbacks."""

from fastapi import FastAPI, Request, Response

from nvidia_build_lb.admin.schemas import AdminEventListResponse, AdminOverviewRead
from nvidia_build_lb.admin_http_errors import safe_credential_error
from nvidia_build_lb.admin_route_shapes import admin_allowed_methods
from nvidia_build_lb.credential_protocols import CredentialRepositorySurface
from nvidia_build_lb.credential_types import InvalidAdminRequestError
from nvidia_build_lb.request_id import request_id_from


def _reject_query(request: Request) -> None:
    if request.url.query:
        raise InvalidAdminRequestError


def _not_found(request: Request) -> Response:
    return safe_credential_error(
        404,
        "resource_not_found",
        "resource not found",
        request_id_from(request),
    )


def register_admin_read_routes(
    app: FastAPI,
    repositories: CredentialRepositorySurface,
) -> None:
    """Register aggregate/event reads and authenticated safe unknown fallbacks."""

    @app.get("/admin/api/v1/overview")
    async def _overview(request: Request) -> AdminOverviewRead:
        _reject_query(request)
        return await repositories.overview()

    @app.get("/admin/api/v1/events")
    async def _events(request: Request) -> AdminEventListResponse:
        _reject_query(request)
        return await repositories.events()

    @app.api_route(
        "/admin/api/v1",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def _unknown_admin_root(request: Request) -> Response:
        return _not_found(request)

    @app.api_route(
        "/admin/api/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def _unknown_admin(request: Request, path: str) -> Response:
        allowed = admin_allowed_methods(f"/admin/api/v1/{path}")
        if allowed is not None and request.method not in allowed:
            return Response(status_code=405, headers={"Allow": ", ".join(sorted(allowed))})
        return _not_found(request)

    _ = (_overview, _events, _unknown_admin_root, _unknown_admin)
