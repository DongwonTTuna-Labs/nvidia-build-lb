"""Shared fail-closed transport constants, errors, and cookie boundary."""

from collections.abc import Iterator
from http.cookiejar import CookieJar
from typing import Final, override

from nvidia_build_lb.pinned_httpx import httpx2

ORIGIN_BASE_URL: Final = "https://integrate.api.nvidia.com"
POLL_BASE_URL: Final = "https://api.nvcf.nvidia.com"
RESPONSE_RAW_HEADERS_EXTENSION: Final = "nvidia_validatable_raw_headers"
ALLOWED_REQUEST_EXTENSIONS: Final = frozenset({"timeout"})
VALIDATABLE_RESPONSE_HEADERS: Final = frozenset(
    {
        b"content-type",
        b"content-length",
        b"content-encoding",
        b"transfer-encoding",
        b"retry-after",
        b"nvcf-reqid",
    }
)
PUBLIC_RESPONSE_HEADERS: Final = (b"content-type", b"content-length", b"retry-after")

_COOKIE_MUTATION_ERROR = "cookie_mutation_forbidden"
_TRANSPORT_DRIFT_ERROR = "pinned_transport_drift"


class CookieMutationError(Exception):
    """Reject every cookie state mutation."""

    @override
    def __str__(self) -> str:
        return _COOKIE_MUTATION_ERROR


class PinnedTransportDriftError(Exception):
    """Reject unapproved transport configuration, types, or exceptions."""

    @override
    def __str__(self) -> str:
        return _TRANSPORT_DRIFT_ERROR


class RejectAllCookieJar(CookieJar):
    """An always-empty jar whose HTTP integration methods access no arguments."""

    @override
    def add_cookie_header(self, request: object) -> None:
        del request

    @override
    def extract_cookies(self, response: object, request: object) -> None:
        del response, request

    @override
    def set_cookie(self, cookie: object, *args: object, **kwargs: object) -> None:
        del cookie, args, kwargs
        raise CookieMutationError


class RejectAllCookies(httpx2.Cookies):
    """An empty immutable httpx2 cookie wrapper."""

    def __init__(self) -> None:
        """Install the rejecting always-empty jar."""
        super().__init__(RejectAllCookieJar())

    @override
    def extract_cookies(self, response: object) -> None:
        del response

    @override
    def set_cookie_header(self, request: object) -> None:
        del request

    @override
    def __len__(self) -> int:
        return 0

    @override
    def __iter__(self) -> Iterator[str]:
        return iter(())

    @override
    def __getitem__(self, _name: str) -> str:
        raise CookieMutationError

    @override
    def __setitem__(self, _name: str, _value: str) -> None:
        raise CookieMutationError

    @override
    def __delitem__(self, _name: str) -> None:
        raise CookieMutationError

    @override
    def set(self, name: str, value: str, domain: str = "", path: str = "/") -> None:
        del name, value, domain, path
        raise CookieMutationError
