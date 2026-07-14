"""Routing transition bundles for public outcome mapping."""

from dataclasses import dataclass

from nvidia_build_lb.outcome_types import RoutingTransition


@dataclass(frozen=True, slots=True)
class Disposition:
    """Bundle alternate eligibility with its exact transition."""

    alternate: bool
    transition: RoutingTransition


PRESERVE = Disposition(alternate=False, transition=RoutingTransition.PRESERVE)
QUARANTINE = Disposition(alternate=True, transition=RoutingTransition.QUARANTINE)
RATE_ALTERNATE = Disposition(alternate=True, transition=RoutingTransition.RATE_COOLDOWN)
TRANSIENT_ALTERNATE = Disposition(
    alternate=True,
    transition=RoutingTransition.TRANSIENT_COOLDOWN,
)
TRANSIENT_TERMINAL = Disposition(
    alternate=False,
    transition=RoutingTransition.TRANSIENT_COOLDOWN,
)
DEGRADE = Disposition(alternate=False, transition=RoutingTransition.DEGRADE_HEALTH_ONLY)
