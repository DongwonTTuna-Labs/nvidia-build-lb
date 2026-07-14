"""Establish an importable migration head before domain tables land."""

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Keep the scaffold head schema-neutral."""


def downgrade() -> None:
    """Keep the scaffold head schema-neutral."""
