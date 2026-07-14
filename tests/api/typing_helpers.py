"""Tiny nominal wrappers that keep test observations explicit and typed."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BodyRecord:
    """One exact JSON body forwarded across the production routing seam."""

    raw: bytes
