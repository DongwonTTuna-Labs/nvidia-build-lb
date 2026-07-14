"""Add durable NVIDIA routing attempts, pins, and cooldown kinds."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_nvidia_routing"
down_revision: str | None = "0002_vault_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CHECK = """
terminal_status_class IS NULL OR terminal_status_class IN (
    'success',
    'invalid_credential',
    'credits_exhausted',
    'rate_limited',
    'request_rejected',
    'timeout',
    'upstream_unavailable',
    'upstream_bad_gateway',
    'upstream_internal_error',
    'upstream_protocol_error',
    'cancelled',
    'delivery_failed'
)
"""


def upgrade() -> None:
    """Upgrade the Todo 2 schema without rewriting existing attempt history."""
    op.add_column("upstream_keys", sa.Column("cooldown_kind", sa.String(16), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE upstream_keys
            SET cooldown_kind = 'transient'
            WHERE cooldown_until IS NOT NULL
            """
        )
    )
    op.create_check_constraint(
        "ck_upstream_cooldown_pair",
        "upstream_keys",
        "(cooldown_until IS NULL) = (cooldown_kind IS NULL)",
    )
    op.create_check_constraint(
        "ck_upstream_cooldown_kind",
        "upstream_keys",
        "cooldown_kind IS NULL OR cooldown_kind IN ('rate_limit','transient')",
    )

    _ = op.create_table(
        "upstream_attempt_receipts",
        sa.Column("started_event_id", sa.Uuid(), nullable=False),
        sa.Column("terminal_event_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_key_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("service_epoch", sa.Uuid(), nullable=False),
        sa.Column("explicit_probe_key_id", sa.Uuid(), nullable=True),
        sa.Column(
            "excluded_key_ids",
            postgresql.ARRAY(sa.Uuid(), dimensions=1),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rate_limit_streak", sa.BigInteger(), nullable=False),
        sa.Column("transient_failure_streak", sa.BigInteger(), nullable=False),
        sa.Column("terminal_outcome", sa.String(16), nullable=True),
        sa.Column("terminal_status_class", sa.String(32), nullable=True),
        sa.Column("terminal_latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("terminal_cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_cooldown_kind", sa.String(16), nullable=True),
        sa.Column("terminal_committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_receipt_state_check(), name="ck_attempt_receipt_state"),
        sa.CheckConstraint("rate_limit_streak >= 0", name="ck_attempt_receipt_rate_streak"),
        sa.CheckConstraint(
            "transient_failure_streak >= 0",
            name="ck_attempt_receipt_transient_streak",
        ),
        sa.CheckConstraint(
            """
            terminal_cooldown_kind IS NULL
            OR terminal_cooldown_kind IN ('rate_limit','transient')
            """,
            name="ck_attempt_receipt_cooldown_kind",
        ),
        sa.CheckConstraint(
            """
            cardinality(excluded_key_ids) <= 1
            AND array_position(excluded_key_ids, NULL) IS NULL
            """,
            name="ck_attempt_receipt_excluded",
        ),
        sa.CheckConstraint(
            """
            explicit_probe_key_id IS NULL
            OR (
                cardinality(excluded_key_ids) = 0
                AND upstream_key_id = explicit_probe_key_id
            )
            """,
            name="ck_attempt_receipt_selection",
        ),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_attempt_receipt_status_class"),
        sa.ForeignKeyConstraint(
            ["started_event_id"],
            ["admin_events.id"],
            name="fk_attempt_receipt_started_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("started_event_id"),
        sa.UniqueConstraint("terminal_event_id", name="uq_attempt_receipt_terminal_event"),
    )
    op.create_index(
        "ix_attempt_receipts_key_started",
        "upstream_attempt_receipts",
        ["upstream_key_id", "started_at", "started_event_id"],
    )

    _ = op.create_table(
        "upstream_live_pins",
        sa.Column("started_event_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_key_id", sa.Uuid(), nullable=False),
        sa.Column("service_epoch", sa.Uuid(), nullable=False),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["started_event_id"],
            ["upstream_attempt_receipts.started_event_id"],
            name="fk_live_pin_attempt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["upstream_key_id"],
            ["upstream_keys.id"],
            name="fk_live_pin_upstream_key",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("started_event_id"),
    )
    op.create_index(
        "ix_live_pins_key",
        "upstream_live_pins",
        ["upstream_key_id", "started_event_id"],
    )
    op.create_index(
        "ix_live_pins_epoch",
        "upstream_live_pins",
        ["service_epoch", "started_event_id"],
    )

    op.add_column(
        "admin_events",
        sa.Column("attempt_started_event_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_admin_event_attempt_receipt",
        "admin_events",
        "upstream_attempt_receipts",
        ["attempt_started_event_id"],
        ["started_event_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_admin_events_attempt_terminal",
        "admin_events",
        ["attempt_started_event_id"],
        unique=True,
        postgresql_where=sa.text("attempt_started_event_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Restore the exact 0002 physical schema."""
    op.drop_index("uq_admin_events_attempt_terminal", table_name="admin_events")
    op.drop_constraint("fk_admin_event_attempt_receipt", "admin_events", type_="foreignkey")
    op.drop_column("admin_events", "attempt_started_event_id")
    op.drop_index("ix_live_pins_epoch", table_name="upstream_live_pins")
    op.drop_index("ix_live_pins_key", table_name="upstream_live_pins")
    op.drop_table("upstream_live_pins")
    op.drop_index("ix_attempt_receipts_key_started", table_name="upstream_attempt_receipts")
    op.drop_table("upstream_attempt_receipts")
    op.drop_constraint("ck_upstream_cooldown_kind", "upstream_keys", type_="check")
    op.drop_constraint("ck_upstream_cooldown_pair", "upstream_keys", type_="check")
    op.drop_column("upstream_keys", "cooldown_kind")


def _receipt_state_check() -> str:
    pending = " AND ".join(
        f"{column} IS NULL"
        for column in (
            "terminal_outcome",
            "terminal_status_class",
            "terminal_latency_ms",
            "terminal_cooldown_until",
            "terminal_cooldown_kind",
            "terminal_committed_at",
        )
    )
    terminal = """
        terminal_outcome IN ('succeeded','failed','cancelled')
        AND terminal_status_class IS NOT NULL
        AND terminal_latency_ms >= 0
        AND terminal_committed_at IS NOT NULL
        AND (
            (terminal_cooldown_until IS NULL AND terminal_cooldown_kind IS NULL)
            OR (terminal_cooldown_until IS NOT NULL AND terminal_cooldown_kind IS NOT NULL)
        )
    """
    return f"(({pending}) OR ({terminal}))"
