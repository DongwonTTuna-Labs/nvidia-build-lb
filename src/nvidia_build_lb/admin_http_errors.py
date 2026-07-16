"""Safe closed exception mapping for the credential administration slice."""

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from nvidia_build_lb.admin.schemas import AdminValidationErrorResponse
from nvidia_build_lb.admin_deadlines import (
    AdminMutationSettlingError,
    AdminReadTimeoutError,
)
from nvidia_build_lb.admin_ledger import (
    LedgerCapacityExhaustedError,
    LedgerStateUnavailableError,
)
from nvidia_build_lb.credential_types import (
    InvalidAdminRequestError,
    ResourceConflictError,
    ResourceNotFoundError,
)
from nvidia_build_lb.request_id import request_id_from
from nvidia_build_lb.schemas import ErrorEnvelope
from nvidia_build_lb.server_error_boundary import (
    response_has_started,
    safe_internal_error_response,
    select_safe_internal_error,
)


def register_credential_error_handlers(app: FastAPI) -> None:
    """Register only fixed safe administration error projections."""
    _register_ledger_error_handlers(app)
    _register_read_error_handlers(app)

    @app.exception_handler(ResourceNotFoundError)
    async def _not_found(request: Request, error: ResourceNotFoundError) -> Response:
        del error
        return safe_credential_error(
            404,
            "resource_not_found",
            "resource not found",
            request_id_from(request),
        )

    @app.exception_handler(ResourceConflictError)
    async def _conflict(request: Request, error: ResourceConflictError) -> Response:
        del error
        return safe_credential_error(
            409,
            "resource_conflict",
            "resource state conflicts",
            request_id_from(request),
        )

    @app.exception_handler(InvalidAdminRequestError)
    async def _invalid_query(request: Request, error: InvalidAdminRequestError) -> Response:
        del error
        return _validation_error(request_id_from(request))

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request: Request, error: RequestValidationError) -> Response:
        del error
        return _validation_error(request_id_from(request))

    @app.exception_handler(SQLAlchemyError)
    async def _database_unavailable(request: Request, error: SQLAlchemyError) -> Response:
        del error
        return safe_credential_error(
            503,
            "database_unavailable",
            "database unavailable",
            request_id_from(request),
        )

    @app.exception_handler(OSError)
    async def _database_connect_unavailable(request: Request, error: OSError) -> Response:
        del error
        return safe_credential_error(
            503,
            "database_unavailable",
            "database unavailable",
            request_id_from(request),
        )

    @app.exception_handler(Exception)
    async def _internal_server_error(request: Request, error: Exception) -> Response:
        del error
        response = safe_internal_error_response(request)
        if not response_has_started(request):
            select_safe_internal_error(request)
        return response

    _ = (
        _not_found,
        _conflict,
        _invalid_query,
        _invalid_body,
        _database_unavailable,
        _database_connect_unavailable,
        _internal_server_error,
    )


def _register_ledger_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LedgerCapacityExhaustedError)
    async def _ledger_capacity(request: Request, error: LedgerCapacityExhaustedError) -> Response:
        del error
        return safe_credential_error(
            503,
            "ledger_capacity_exhausted",
            "request evidence capacity exhausted",
            request_id_from(request),
        )

    @app.exception_handler(LedgerStateUnavailableError)
    async def _ledger_unavailable(request: Request, error: LedgerStateUnavailableError) -> Response:
        del error
        return safe_credential_error(
            503,
            "database_unavailable",
            "database unavailable",
            request_id_from(request),
        )

    _ = (_ledger_capacity, _ledger_unavailable)


def _register_read_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AdminReadTimeoutError)
    async def _read_timeout(request: Request, error: AdminReadTimeoutError) -> Response:
        del error
        return safe_credential_error(
            504,
            "admin_read_timeout",
            "current snapshot not confirmed",
            request_id_from(request),
        )

    @app.exception_handler(AdminMutationSettlingError)
    async def _mutation_settling(request: Request, error: AdminMutationSettlingError) -> Response:
        del error
        return safe_credential_error(
            503,
            "admin_mutation_settling",
            "mutation settlement not confirmed",
            request_id_from(request),
        )

    _ = (_read_timeout, _mutation_settling)


def safe_credential_error(
    status_code: int,
    code: str,
    message: str,
    request_id: str,
) -> Response:
    """Build one fixed safe credential API error."""
    return Response(
        status_code=status_code,
        content=ErrorEnvelope.from_safe_parts(code, message, request_id).model_dump_json(),
        media_type="application/json",
    )


def _validation_error(request_id: str) -> Response:
    return Response(
        status_code=422,
        content=AdminValidationErrorResponse.from_request_id(request_id).model_dump_json(),
        media_type="application/json",
    )
