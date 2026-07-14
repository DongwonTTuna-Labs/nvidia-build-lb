"""Pure routing-state transitions applied by durable finalization."""

from nvidia_build_lb.admin.schemas import HealthState, LastStatusClass
from nvidia_build_lb.attempt_types import AttemptFinalizeCommand
from nvidia_build_lb.cooldown_pairs import merge_cooldown_pair
from nvidia_build_lb.db_models import UpstreamKeyRow
from nvidia_build_lb.quarantine_recovery import terminal_updates_routing_state


def apply_terminal_transition(
    key: UpstreamKeyRow,
    command: AttemptFinalizeCommand,
    *,
    explicit_probe: bool = False,
) -> None:
    """Apply exactly one closed terminal transition to a locked key row."""
    status = command.status_class
    if not terminal_updates_routing_state(key, status, explicit_probe=explicit_probe):
        return
    if status is LastStatusClass.SUCCESS:
        key.health_state = HealthState.HEALTHY.value
        key.quarantined = False
        key.cooldown_until = None
        key.cooldown_kind = None
        key.consecutive_rate_limits = 0
        key.consecutive_transient_failures = 0
        return
    if status in {
        LastStatusClass.INVALID_CREDENTIAL,
        LastStatusClass.CREDITS_EXHAUSTED,
    }:
        key.health_state = HealthState.DEGRADED.value
        key.quarantined = True
        return
    if status is LastStatusClass.UPSTREAM_PROTOCOL_ERROR:
        key.health_state = HealthState.DEGRADED.value
        return
    if status is LastStatusClass.RATE_LIMITED:
        cooldown_kind = command.cooldown_kind
        if cooldown_kind is None:
            raise RuntimeError
        key.health_state = HealthState.DEGRADED.value
        key.cooldown_until, key.cooldown_kind = merge_cooldown_pair(
            key.cooldown_until,
            key.cooldown_kind,
            command.cooldown_until,
            cooldown_kind.value,
        )
        key.consecutive_rate_limits += 1
        key.consecutive_transient_failures = 0
        return
    if (
        status
        in {
            LastStatusClass.TIMEOUT,
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            LastStatusClass.UPSTREAM_BAD_GATEWAY,
        }
        and command.cooldown_until is not None
    ):
        cooldown_kind = command.cooldown_kind
        if cooldown_kind is None:
            raise RuntimeError
        key.health_state = HealthState.DEGRADED.value
        key.cooldown_until, key.cooldown_kind = merge_cooldown_pair(
            key.cooldown_until,
            key.cooldown_kind,
            command.cooldown_until,
            cooldown_kind.value,
        )
        key.consecutive_transient_failures += 1
        key.consecutive_rate_limits = 0
