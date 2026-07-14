"""Atomic cooldown deadline and reason merge shared by both schedulers."""

from datetime import datetime

_RATE_LIMIT = "rate_limit"
_TRANSIENT = "transient"


def merge_cooldown_pair(
    current_until: datetime | None,
    current_kind: str | None,
    observed_until: datetime | None,
    observed_kind: str,
) -> tuple[datetime | None, str | None]:
    """Keep the later pair and prefer rate-limit when deadlines tie."""
    if observed_until is None:
        return current_until, current_kind
    if current_until is None or observed_until > current_until:
        return observed_until, observed_kind
    if observed_until < current_until:
        return current_until, current_kind
    if _RATE_LIMIT in (current_kind, observed_kind):
        return current_until, _RATE_LIMIT
    return current_until, _TRANSIENT
