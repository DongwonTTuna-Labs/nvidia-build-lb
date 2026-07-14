"""Stable full-ring traversal shared by both durable scheduler surfaces."""

from uuid import UUID

from nvidia_build_lb.db_models import UpstreamKeyRow


def next_ring_key(
    rows: tuple[UpstreamKeyRow, ...],
    eligible: tuple[UpstreamKeyRow, ...],
    cursor: UUID | None,
) -> UpstreamKeyRow:
    """Select the first eligible successor while retaining ineligible ring anchors."""
    eligible_ids = {row.id for row in eligible}
    for index, row in enumerate(rows):
        if row.id == cursor:
            for offset in range(1, len(rows) + 1):
                candidate = rows[(index + offset) % len(rows)]
                if candidate.id in eligible_ids:
                    return candidate
    return eligible[0]
