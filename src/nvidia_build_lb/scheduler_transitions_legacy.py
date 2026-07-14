"""Legacy scheduler terminal transitions with conservative probe recovery."""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Final

from nvidia_build_lb.admin.schemas import HealthState, LastStatusClass
from nvidia_build_lb.cooldown_pairs import merge_cooldown_pair
from nvidia_build_lb.db_models import UpstreamKeyRow
from nvidia_build_lb.quarantine_recovery import terminal_updates_routing_state
from nvidia_build_lb.scheduler_types import AttemptTerminal


def _mark_success(key: UpstreamKeyRow, _terminal: AttemptTerminal) -> None:
    key.health_state = HealthState.HEALTHY.value
    key.cooldown_until = None
    key.cooldown_kind = None
    key.quarantined = False
    key.consecutive_rate_limits = 0
    key.consecutive_transient_failures = 0


def _quarantine(key: UpstreamKeyRow, _terminal: AttemptTerminal) -> None:
    key.health_state = HealthState.DEGRADED.value
    key.quarantined = True


def _mark_rate_limited(key: UpstreamKeyRow, terminal: AttemptTerminal) -> None:
    key.health_state = HealthState.DEGRADED.value
    key.cooldown_until, key.cooldown_kind = merge_cooldown_pair(
        key.cooldown_until,
        key.cooldown_kind,
        terminal.cooldown_until,
        "rate_limit",
    )
    key.consecutive_rate_limits += 1
    key.consecutive_transient_failures = 0


def _mark_transient_failure(key: UpstreamKeyRow, terminal: AttemptTerminal) -> None:
    if terminal.cooldown_until is not None:
        key.health_state = HealthState.DEGRADED.value
        key.cooldown_until, key.cooldown_kind = merge_cooldown_pair(
            key.cooldown_until,
            key.cooldown_kind,
            terminal.cooldown_until,
            "transient",
        )
        key.consecutive_transient_failures += 1
        key.consecutive_rate_limits = 0


def _preserve_routing_state(key: UpstreamKeyRow, terminal: AttemptTerminal) -> None:
    if terminal.status_class is LastStatusClass.UPSTREAM_PROTOCOL_ERROR:
        key.health_state = HealthState.DEGRADED.value


_TRANSITIONS: Final[Mapping[LastStatusClass, Callable[[UpstreamKeyRow, AttemptTerminal], None]]] = (
    MappingProxyType(
        {
            LastStatusClass.SUCCESS: _mark_success,
            LastStatusClass.INVALID_CREDENTIAL: _quarantine,
            LastStatusClass.CREDITS_EXHAUSTED: _quarantine,
            LastStatusClass.RATE_LIMITED: _mark_rate_limited,
            LastStatusClass.TIMEOUT: _mark_transient_failure,
            LastStatusClass.UPSTREAM_UNAVAILABLE: _mark_transient_failure,
            LastStatusClass.UPSTREAM_BAD_GATEWAY: _mark_transient_failure,
            LastStatusClass.REQUEST_REJECTED: _preserve_routing_state,
            LastStatusClass.UPSTREAM_INTERNAL_ERROR: _preserve_routing_state,
            LastStatusClass.UPSTREAM_PROTOCOL_ERROR: _preserve_routing_state,
            LastStatusClass.CANCELLED: _preserve_routing_state,
            LastStatusClass.DELIVERY_FAILED: _preserve_routing_state,
        }
    )
)


def apply_legacy_terminal_transition(
    key: UpstreamKeyRow,
    terminal: AttemptTerminal,
    *,
    explicit_probe: bool,
) -> None:
    """Apply one terminal while allowing only explicit probes to recover quarantine."""
    if not terminal_updates_routing_state(
        key,
        terminal.status_class,
        explicit_probe=explicit_probe,
    ):
        return
    _TRANSITIONS[terminal.status_class](key, terminal)
