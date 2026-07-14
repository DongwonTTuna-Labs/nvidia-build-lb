"""Atomic cooldown-pair merge precedence under terminal row locks."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from nvidia_build_lb.admin.schemas import HealthState, LastStatusClass
from nvidia_build_lb.attempt_transitions import apply_terminal_transition
from nvidia_build_lb.attempt_types import (
    AttemptFinalizeCommand,
    AttemptIdentity,
    CooldownKind,
)
from nvidia_build_lb.db_models import UpstreamKeyRow
from nvidia_build_lb.scheduler_state import TerminalOutcome
from nvidia_build_lb.scheduler_transitions_legacy import apply_legacy_terminal_transition
from nvidia_build_lb.scheduler_types import AttemptTerminal

pytestmark = pytest.mark.nvidia_routing

_NOW = datetime(2026, 7, 13, tzinfo=UTC)


def _key(kind: CooldownKind, until: datetime) -> UpstreamKeyRow:
    return UpstreamKeyRow(
        id=uuid4(),
        fingerprint="a" * 64,
        vault_version=1,
        vault_nonce=b"n" * 12,
        vault_ciphertext=b"c" * 17,
        enabled=True,
        health_state=HealthState.DEGRADED.value,
        cooldown_until=until,
        cooldown_kind=kind.value,
        quarantined=False,
        request_count=1,
        success_count=0,
        failure_count=0,
        consecutive_rate_limits=0,
        consecutive_transient_failures=0,
        last_status_class=None,
        last_used_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _command(
    status: LastStatusClass, kind: CooldownKind, until: datetime
) -> AttemptFinalizeCommand:
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)
    return AttemptFinalizeCommand(
        identity=identity,
        outcome=TerminalOutcome.FAILED,
        status_class=status,
        latency_ms=1,
        cooldown_until=until,
        cooldown_kind=kind,
        terminal_committed_at=_NOW,
    )


def test_later_existing_rate_cooldown_is_not_shortened_or_reclassified() -> None:
    key = _key(CooldownKind.RATE_LIMIT, _NOW + timedelta(seconds=10))

    apply_terminal_transition(
        key,
        _command(
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            CooldownKind.TRANSIENT,
            _NOW + timedelta(seconds=5),
        ),
    )

    assert key.cooldown_until == _NOW + timedelta(seconds=10)
    assert key.cooldown_kind == CooldownKind.RATE_LIMIT.value


@pytest.mark.parametrize(
    ("existing", "incoming_status", "incoming"),
    [
        (CooldownKind.TRANSIENT, LastStatusClass.RATE_LIMITED, CooldownKind.RATE_LIMIT),
        (
            CooldownKind.RATE_LIMIT,
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            CooldownKind.TRANSIENT,
        ),
    ],
)
def test_exact_expiry_tie_always_selects_rate_limit_kind(
    existing: CooldownKind,
    incoming_status: LastStatusClass,
    incoming: CooldownKind,
) -> None:
    until = _NOW + timedelta(seconds=10)
    key = _key(existing, until)

    apply_terminal_transition(key, _command(incoming_status, incoming, until))

    assert key.cooldown_until == until
    assert key.cooldown_kind == CooldownKind.RATE_LIMIT.value


def test_later_transient_cooldown_replaces_earlier_rate_pair_atomically() -> None:
    key = _key(CooldownKind.RATE_LIMIT, _NOW + timedelta(seconds=5))
    observed = _NOW + timedelta(seconds=10)

    apply_terminal_transition(
        key,
        _command(
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            CooldownKind.TRANSIENT,
            observed,
        ),
    )

    assert key.cooldown_until == observed
    assert key.cooldown_kind == CooldownKind.TRANSIENT.value


def test_only_explicit_probe_success_recovers_existing_quarantine() -> None:
    key = _key(CooldownKind.TRANSIENT, _NOW + timedelta(seconds=10))
    key.quarantined = True
    success = AttemptFinalizeCommand.success(
        AttemptIdentity(uuid4(), uuid4(), "late-public-success", uuid4(), _NOW),
        _NOW,
        latency_ms=1,
    )

    apply_terminal_transition(key, success)

    assert key.quarantined is True
    assert key.health_state == HealthState.DEGRADED.value
    assert key.cooldown_until == _NOW + timedelta(seconds=10)
    assert key.cooldown_kind == CooldownKind.TRANSIENT.value

    apply_terminal_transition(key, success, explicit_probe=True)

    assert key.quarantined is False
    assert key.health_state == HealthState.HEALTHY.value
    assert key.cooldown_until is None
    assert key.cooldown_kind is None


def test_legacy_delivery_failure_preserves_routing_state_without_key_error() -> None:
    until = _NOW + timedelta(seconds=10)
    key = _key(CooldownKind.TRANSIENT, until)

    apply_legacy_terminal_transition(
        key,
        AttemptTerminal(
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.DELIVERY_FAILED,
            latency_ms=1,
            cooldown_until=None,
        ),
        explicit_probe=False,
    )

    assert key.health_state == HealthState.DEGRADED.value
    assert key.cooldown_until == until
    assert key.cooldown_kind == CooldownKind.TRANSIENT.value


@pytest.mark.parametrize(
    ("existing_kind", "incoming_status", "incoming_seconds", "expected_kind"),
    [
        (
            CooldownKind.RATE_LIMIT,
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            5,
            CooldownKind.RATE_LIMIT,
        ),
        (
            CooldownKind.TRANSIENT,
            LastStatusClass.RATE_LIMITED,
            5,
            CooldownKind.TRANSIENT,
        ),
        (
            CooldownKind.RATE_LIMIT,
            LastStatusClass.UPSTREAM_UNAVAILABLE,
            10,
            CooldownKind.RATE_LIMIT,
        ),
        (
            CooldownKind.TRANSIENT,
            LastStatusClass.RATE_LIMITED,
            10,
            CooldownKind.RATE_LIMIT,
        ),
    ],
)
def test_legacy_mixed_cooldown_merge_keeps_deadline_and_reason_atomic(
    existing_kind: CooldownKind,
    incoming_status: LastStatusClass,
    incoming_seconds: int,
    expected_kind: CooldownKind,
) -> None:
    until = _NOW + timedelta(seconds=10)
    key = _key(existing_kind, until)

    apply_legacy_terminal_transition(
        key,
        AttemptTerminal(
            outcome=TerminalOutcome.FAILED,
            status_class=incoming_status,
            latency_ms=1,
            cooldown_until=_NOW + timedelta(seconds=incoming_seconds),
        ),
        explicit_probe=False,
    )

    assert key.cooldown_until == until
    assert key.cooldown_kind == expected_kind.value


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (LastStatusClass.RATE_LIMITED, CooldownKind.TRANSIENT),
        (LastStatusClass.UPSTREAM_UNAVAILABLE, CooldownKind.RATE_LIMIT),
        (LastStatusClass.SUCCESS, CooldownKind.TRANSIENT),
    ],
)
def test_finalize_command_rejects_status_cooldown_kind_mismatch(
    status: LastStatusClass,
    kind: CooldownKind,
) -> None:
    with pytest.raises(ValueError, match="invalid_cooldown_kind_for_status"):
        _ = _command(status, kind, _NOW + timedelta(seconds=1))


def test_rate_limited_finalize_requires_explicit_rate_cooldown_pair() -> None:
    identity = AttemptIdentity(uuid4(), uuid4(), "request", uuid4(), _NOW)

    with pytest.raises(ValueError, match="rate_limit_cooldown_required"):
        _ = AttemptFinalizeCommand(
            identity=identity,
            outcome=TerminalOutcome.FAILED,
            status_class=LastStatusClass.RATE_LIMITED,
            latency_ms=1,
            cooldown_until=None,
            cooldown_kind=None,
            terminal_committed_at=_NOW,
        )
