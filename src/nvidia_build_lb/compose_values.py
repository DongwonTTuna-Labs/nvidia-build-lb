"""Strict coercion for non-secret decimal values emitted by Compose."""

from typing import Final

_MAX_DECIMAL_DIGITS: Final = 10


def parse_canonical_decimal(value: object) -> object:
    """Convert only a bounded, leading-zero-free ASCII decimal string."""
    if (
        isinstance(value, str)
        and len(value) <= _MAX_DECIMAL_DIGITS
        and not value.startswith("0")
        and value.isascii()
        and value.isdecimal()
    ):
        return int(value)
    return value
