"""Safe public error serialization for routed failures and model rejection."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from fastapi import Response

from nvidia_build_lb.polling import FailureTerminal
from nvidia_build_lb.schemas import ErrorEnvelope

_FALLBACK_STATUS: Final = 503
_FALLBACK_CODE: Final = "upstream_unavailable"
_FALLBACK_MESSAGE: Final = "upstream is unavailable"
_MODEL_ERROR: Final[tuple[int, str, str]] = (
    404,
    "model_not_found",
    "requested model was not found",
)
_JSON_HEADERS: Final[Mapping[str, str]] = MappingProxyType({"Cache-Control": "no-store"})


def model_not_found(request_id: str) -> Response:
    """Return the one fixed-model rejection."""
    return _safe_error(*_MODEL_ERROR, request_id=request_id)


def routed_failure(terminal: FailureTerminal, request_id: str) -> Response:
    """Serialize only the closed public outcome, never an upstream body."""
    outcome = terminal.outcome
    status = outcome.http_status or _FALLBACK_STATUS
    code = outcome.code or _FALLBACK_CODE
    message = outcome.message or _FALLBACK_MESSAGE
    headers = dict(_JSON_HEADERS)
    if terminal.retry_after_seconds is not None:
        headers["Retry-After"] = str(terminal.retry_after_seconds)
    return _safe_error(status, code, message, request_id=request_id, headers=headers)


def _safe_error(
    status_code: int,
    code: str,
    message: str,
    *,
    request_id: str,
    headers: Mapping[str, str] = _JSON_HEADERS,
) -> Response:
    return Response(
        status_code=status_code,
        content=ErrorEnvelope.from_safe_parts(code, message, request_id).model_dump_json(),
        media_type="application/json",
        headers=headers,
    )
