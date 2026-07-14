"""Canonical lowercase hyphenated UUID wire parsing."""

from uuid import UUID

from nvidia_build_lb.credential_types import InvalidAdminRequestError


def parse_canonical_uuid(raw: str) -> UUID:
    """Return only the one canonical string representation of a UUID."""
    try:
        parsed = UUID(raw)
    except ValueError:
        raise InvalidAdminRequestError from None
    if str(parsed) != raw:
        raise InvalidAdminRequestError
    return parsed
