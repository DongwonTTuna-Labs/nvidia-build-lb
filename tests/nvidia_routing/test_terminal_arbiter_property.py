"""Total terminal order, duplicate collapse, and losing-CAS properties."""

from itertools import combinations

import pytest

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.terminal import TerminalArbiter, TerminalKind, TerminalProposal

pytestmark = pytest.mark.nvidia_routing


def _proposal(kind: TerminalKind, sequence: int) -> TerminalProposal:
    return TerminalProposal.for_kind(kind, sequence=sequence)


_PAIR_CASES = tuple(
    pytest.param(first, second, order, id=f"{first.value}__{second.value}__{order}")
    for first, second in combinations(tuple(TerminalKind), 2)
    for order in ("AB", "BA")
)


@pytest.mark.parametrize(("first", "second", "order"), _PAIR_CASES)
def test_total_order_pair(first: TerminalKind, second: TerminalKind, order: str) -> None:
    proposals = (
        (_proposal(first, 1), _proposal(second, 2))
        if order == "AB"
        else (_proposal(second, 2), _proposal(first, 1))
    )

    decision = TerminalArbiter().register_batch(proposals)

    assert decision.winner.kind is first
    assert decision.cas_success is True
    assert decision.discarded_count == 1


_AXES = (
    "terminal_arbiter_same_discriminator_rule_canonical_tuple",
    "terminal_arbiter_same_discriminator_rule_exact_duplicates",
    "terminal_arbiter_same_discriminator_rule_input_order_dependency",
    "terminal_arbiter_same_discriminator_rule_nonidentical_duplicates",
    "terminal_arbiter_payload_creator",
    "terminal_arbiter_losing_CAS",
)


@pytest.mark.parametrize("axis", _AXES, ids=_AXES)
def test_atomic_accepted_axis(axis: str) -> None:
    low = TerminalProposal.network_failure(
        sequence=1,
        status_class=LastStatusClass.UPSTREAM_UNAVAILABLE,
        payload=b"a",
    )
    high = TerminalProposal.network_failure(
        sequence=2,
        status_class=LastStatusClass.UPSTREAM_UNAVAILABLE,
        payload=b"z",
    )
    exact = TerminalArbiter().register_batch((low, low))
    forward = TerminalArbiter().register_batch((high, low))
    reverse = TerminalArbiter().register_batch((low, high))
    arbiter = TerminalArbiter()
    winner = arbiter.register_one(low)
    loser = arbiter.register_one(high)

    observations = {
        "terminal_arbiter_same_discriminator_rule_canonical_tuple": (
            low.canonical_tuple()[0] == TerminalKind.NETWORK_TERMINAL_FAILURE.value
            and low.canonical_tuple() < high.canonical_tuple()
        ),
        "terminal_arbiter_same_discriminator_rule_exact_duplicates": (
            exact.winner == low and exact.discarded_count == 1
        ),
        "terminal_arbiter_same_discriminator_rule_input_order_dependency": (
            forward.winner == reverse.winner == low
        ),
        "terminal_arbiter_same_discriminator_rule_nonidentical_duplicates": (
            forward.winner == low and forward.discarded_count == 1
        ),
        "terminal_arbiter_payload_creator": (
            winner.winner.payload == b"a" and winner.winner.payload_kind == "canonical_local_error"
        ),
        "terminal_arbiter_losing_CAS": (loser.cas_success is False and loser.winner == low),
    }
    assert observations[axis] is True
