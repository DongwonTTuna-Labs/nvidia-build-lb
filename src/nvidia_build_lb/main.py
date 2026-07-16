"""Import-safe FastAPI composition root for the NVIDIA-only gateway."""

import orjson
from fastapi import FastAPI, Response

from nvidia_build_lb.admin_credentials import register_credential_routes
from nvidia_build_lb.admin_http_errors import register_credential_error_handlers
from nvidia_build_lb.admin_mutation_boundary import AdminMutationBoundaryMiddleware
from nvidia_build_lb.api_routes import register_public_routes
from nvidia_build_lb.api_types import ApplicationServices
from nvidia_build_lb.auth import CredentialAuthMiddleware
from nvidia_build_lb.request_boundary import RequestBoundaryMiddleware
from nvidia_build_lb.request_id import RequestIdMiddleware
from nvidia_build_lb.server_error_boundary import SafeServerErrorFastAPI
from nvidia_build_lb.web.admin_resources import create_admin_resource_router


def _register_unconfigured_health(application: FastAPI) -> None:
    @application.get("/health", response_class=Response)
    async def _health() -> Response:
        return Response(
            status_code=503,
            content=orjson.dumps({"status": "degraded", "ready": False}),
            media_type="application/json",
        )

    _ = _health


def create_app(services: ApplicationServices | None = None) -> SafeServerErrorFastAPI:
    """Compose exact resources and optionally the injected runtime services."""
    application = SafeServerErrorFastAPI(
        title="NVIDIA Build LB",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    application.include_router(create_admin_resource_router())
    register_credential_error_handlers(application)
    if services is None:
        _register_unconfigured_health(application)
    else:
        register_credential_routes(
            application,
            services.credentials,
            include_scope_test_routes=False,
        )
        register_public_routes(application, services)
        application.add_middleware(
            AdminMutationBoundaryMiddleware,
            barrier=services.credentials.mutation_barrier,
            lifecycle=services.credentials.lifecycle,
            deadline_seconds=services.credentials.admin_mutation_deadline_seconds,
        )
        application.add_middleware(
            CredentialAuthMiddleware,
            authenticators=services.credentials.authenticators,
        )
    application.add_middleware(RequestIdMiddleware)
    application.add_middleware(RequestBoundaryMiddleware)
    return application


app = create_app()
