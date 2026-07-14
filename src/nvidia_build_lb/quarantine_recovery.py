"""Shared conservative quarantine recovery decision for both scheduler surfaces."""

from nvidia_build_lb.admin.schemas import LastStatusClass
from nvidia_build_lb.db_models import UpstreamKeyRow


def terminal_updates_routing_state(
    key: UpstreamKeyRow,
    status: LastStatusClass,
    *,
    explicit_probe: bool,
) -> bool:
    """Allow quarantine recovery metadata only for an explicit successful probe."""
    return not (status is LastStatusClass.SUCCESS and key.quarantined and not explicit_probe)
