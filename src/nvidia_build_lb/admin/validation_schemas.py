"""Safe fixed-shape administration validation-error DTOs."""

from typing import Literal, Self

from nvidia_build_lb.admin._types import OpaqueIdentifier
from nvidia_build_lb.admin.schema_base import AdminDTO


class _AdminValidationErrorDetail(AdminDTO):
    code: Literal["invalid_request"] = "invalid_request"
    message: Literal["request validation failed"] = "request validation failed"
    request_id: OpaqueIdentifier


class AdminValidationErrorResponse(AdminDTO):
    """Fixed safe replacement for framework validation detail."""

    error: _AdminValidationErrorDetail

    @classmethod
    def from_request_id(cls, request_id: str) -> Self:
        """Construct the sole safe admin 422 response."""
        return cls(error=_AdminValidationErrorDetail(request_id=request_id))
