"""Uncomposed credential API factory for isolated Todo 2 verification."""

from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from fastapi import FastAPI, Request, Response

from nvidia_build_lb import credential_repositories
from nvidia_build_lb.admin.schemas import (
    DownstreamTokenIssued,
    DownstreamTokenIssueRequest,
    DownstreamTokenListResponse,
    UpstreamKeyCreateRequest,
    UpstreamKeyListResponse,
    UpstreamKeyRead,
    UpstreamProbeResponse,
)
from nvidia_build_lb.admin_deadlines import run_admin_read
from nvidia_build_lb.admin_http_errors import register_credential_error_handlers
from nvidia_build_lb.admin_mutation_barrier import AdminMutationBarrier
from nvidia_build_lb.admin_mutation_boundary import (
    AdminMutationBoundaryMiddleware,
    AlwaysOperationalLifecycle,
    LifecycleAvailability,
)
from nvidia_build_lb.admin_read_routes import register_admin_read_routes
from nvidia_build_lb.admin_scope_test_routes import register_scope_test_routes
from nvidia_build_lb.auth import CredentialAuthenticators, CredentialAuthMiddleware
from nvidia_build_lb.canonical_uuid import parse_canonical_uuid
from nvidia_build_lb.credential_protocols import CredentialRepositorySurface
from nvidia_build_lb.credential_types import InvalidAdminRequestError
from nvidia_build_lb.request_boundary import RequestBoundaryMiddleware
from nvidia_build_lb.request_id import RequestIdMiddleware, request_id_from

CredentialRepositories = credential_repositories.CredentialRepositories


class ProbeExecutor(Protocol):
    """Perform one externally owned probe and return its safe projection."""

    async def probe(self, key_id: UUID, request_id: str) -> UpstreamProbeResponse:
        """Probe only the requested key without changing its enabled state."""
        ...


@dataclass(frozen=True, slots=True)
class CredentialServices:
    """Bundle repositories, authenticators, and an injected probe seam."""

    repositories: CredentialRepositorySurface
    authenticators: CredentialAuthenticators
    probe: ProbeExecutor
    lifecycle: LifecycleAvailability = field(default_factory=AlwaysOperationalLifecycle)
    mutation_barrier: AdminMutationBarrier = field(default_factory=AdminMutationBarrier)
    admin_read_deadline_seconds: float = 5
    admin_mutation_deadline_seconds: int = 125


def _reject_query(request: Request) -> None:
    if request.url.query:
        raise InvalidAdminRequestError


def _register_upstream_routes(app: FastAPI, services: CredentialServices) -> None:
    @app.get("/admin/api/v1/upstream-keys")
    async def _upstream_list(request: Request) -> UpstreamKeyListResponse:
        _reject_query(request)
        operation = services.repositories.upstream.list_all
        return await run_admin_read(operation, services.admin_read_deadline_seconds)

    @app.post(
        "/admin/api/v1/upstream-keys",
        status_code=201,
    )
    async def _upstream_create(
        request: Request,
        payload: UpstreamKeyCreateRequest,
    ) -> UpstreamKeyRead:
        _reject_query(request)
        return await services.repositories.upstream.create(payload, request_id_from(request))

    @app.post("/admin/api/v1/upstream-keys/{key_id}/enable", status_code=204)
    async def _upstream_enable(request: Request, key_id: str) -> Response:
        _reject_query(request)
        await services.repositories.upstream.enable(
            parse_canonical_uuid(key_id),
            request_id_from(request),
        )
        return Response(status_code=204)

    @app.post("/admin/api/v1/upstream-keys/{key_id}/disable", status_code=204)
    async def _upstream_disable(request: Request, key_id: str) -> Response:
        _reject_query(request)
        await services.repositories.upstream.disable(
            parse_canonical_uuid(key_id),
            request_id_from(request),
        )
        return Response(status_code=204)

    @app.post("/admin/api/v1/upstream-keys/{key_id}/probe")
    async def _upstream_probe(request: Request, key_id: str) -> UpstreamProbeResponse:
        _reject_query(request)
        return await services.probe.probe(
            parse_canonical_uuid(key_id),
            request_id_from(request),
        )

    @app.delete("/admin/api/v1/upstream-keys/{key_id}", status_code=204)
    async def _upstream_delete(request: Request, key_id: str) -> Response:
        _reject_query(request)
        await services.repositories.upstream.delete(
            parse_canonical_uuid(key_id),
            request_id_from(request),
        )
        return Response(status_code=204)

    _ = (
        _upstream_list,
        _upstream_create,
        _upstream_enable,
        _upstream_disable,
        _upstream_probe,
        _upstream_delete,
    )


def _register_downstream_routes(app: FastAPI, services: CredentialServices) -> None:
    @app.get("/admin/api/v1/downstream-tokens")
    async def _downstream_list(request: Request) -> DownstreamTokenListResponse:
        _reject_query(request)
        operation = services.repositories.downstream.list_all
        return await run_admin_read(operation, services.admin_read_deadline_seconds)

    @app.post(
        "/admin/api/v1/downstream-tokens",
        status_code=201,
    )
    async def _downstream_issue(
        request: Request,
        payload: DownstreamTokenIssueRequest,
    ) -> DownstreamTokenIssued:
        _reject_query(request)
        return await services.repositories.downstream.issue(payload, request_id_from(request))

    @app.delete("/admin/api/v1/downstream-tokens/{token_id}", status_code=204)
    async def _downstream_revoke(request: Request, token_id: str) -> Response:
        _reject_query(request)
        await services.repositories.downstream.revoke(
            parse_canonical_uuid(token_id),
            request_id_from(request),
        )
        return Response(status_code=204)

    _ = (_downstream_list, _downstream_issue, _downstream_revoke)


def register_credential_routes(
    app: FastAPI,
    services: CredentialServices,
    *,
    include_scope_test_routes: bool,
) -> None:
    """Register admin routes and optional Todo 2 public auth probes."""
    _register_upstream_routes(app, services)
    _register_downstream_routes(app, services)
    register_admin_read_routes(
        app,
        services.repositories,
        deadline_seconds=services.admin_read_deadline_seconds,
    )
    if include_scope_test_routes:
        register_scope_test_routes(app)


def create_credential_test_app(services: CredentialServices) -> FastAPI:
    """Build only the isolated credential routes used by Todo 2 QA."""
    app = FastAPI(
        title="NVIDIA Build LB credential slice",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    register_credential_error_handlers(app)
    register_credential_routes(app, services, include_scope_test_routes=True)

    app.add_middleware(
        AdminMutationBoundaryMiddleware,
        barrier=services.mutation_barrier,
        lifecycle=services.lifecycle,
        deadline_seconds=services.admin_mutation_deadline_seconds,
    )
    app.add_middleware(
        CredentialAuthMiddleware,
        authenticators=services.authenticators,
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(RequestBoundaryMiddleware)
    return app
