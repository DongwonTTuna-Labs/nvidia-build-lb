"""Provider cookies are structurally disabled without argument access."""

from typing import override

import pytest

from nvidia_build_lb.transport import (
    CookieMutationError,
    RejectAllCookieJar,
    RejectAllCookies,
)

pytestmark = pytest.mark.nvidia_routing


class _ExplodingSentinel:
    @override
    def __getattribute__(self, _name: str) -> object:
        raise AssertionError

    def __iter__(self) -> object:
        raise AssertionError

    @override
    def __repr__(self) -> str:
        raise AssertionError


def test_cookie_jar_add_and_extract_are_noop_without_argument_access() -> None:
    jar = RejectAllCookieJar()
    sentinel = _ExplodingSentinel()

    jar.add_cookie_header(sentinel)
    jar.extract_cookies(sentinel, sentinel)

    assert list(jar) == []


def test_cookie_wrapper_is_empty_and_noop_without_response_or_request_access() -> None:
    cookies = RejectAllCookies()
    sentinel = _ExplodingSentinel()

    cookies.extract_cookies(sentinel)
    cookies.set_cookie_header(sentinel)

    assert len(cookies) == 0
    assert list(cookies) == []


def test_cookie_mutators_fail_closed() -> None:
    cookies = RejectAllCookies()

    with pytest.raises(CookieMutationError):
        cookies.set("poison", "secret")
    with pytest.raises(CookieMutationError):
        cookies["poison"] = "secret"
    with pytest.raises(CookieMutationError):
        del cookies["poison"]
