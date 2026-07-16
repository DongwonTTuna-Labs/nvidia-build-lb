"""Add coherent dashboard identity, exact linkage, and bounded ledger state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_admin_dashboard_ledger"
down_revision: str | None = "0004_vault_key_verifier"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAX_EVENTS = 1_000_000
_MAX_RECEIPTS = 400_000
_MAX_LIVE_PINS = 400_000
_ROLLUP_LOSS = "admin_ledger_rollup_would_be_lost"
_FINGERPRINT_LOSS = "deleted_key_fingerprint_would_be_lost"
_EVENT_LIMIT = "migration_preflight_admin_events_limit"
_RECEIPT_LIMIT = "migration_preflight_upstream_attempt_receipts_limit"
_PIN_LIMIT = "migration_preflight_upstream_live_pins_limit"

_LEDGER_SINGLETON = sa.column("singleton_id", sa.SmallInteger())
_LEDGER_ROLLED_UP = sa.column("rolled_up_routed_request_count", sa.BigInteger())
_LEDGER_TABLE = sa.table("admin_ledger_state", _LEDGER_SINGLETON, _LEDGER_ROLLED_UP)
_EVENT_KEY_ID = sa.column("upstream_key_id", sa.Uuid())
_EVENT_FINGERPRINT = sa.column("upstream_key_fingerprint", sa.String())
_EVENT_TABLE = sa.table("admin_events", _EVENT_KEY_ID, _EVENT_FINGERPRINT)
_UPSTREAM_KEY_ID = sa.column("id", sa.Uuid())
_UPSTREAM_KEY_TABLE = sa.table("upstream_keys", _UPSTREAM_KEY_ID)
_RECEIPT_TABLE = sa.table("upstream_attempt_receipts")
_LIVE_PIN_TABLE = sa.table("upstream_live_pins")


def upgrade() -> None:
    """Apply the bounded forward-only dashboard and ledger schema delta."""
    _bounded_preflight()

    op.add_column(
        "admin_events",
        sa.Column("upstream_key_fingerprint", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "admin_events",
        sa.Column("writer_generation", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_admin_event_writer_generation",
        "admin_events",
        "writer_generation IS NOT NULL AND writer_generation = 5",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        "ck_admin_event_fingerprint_pair",
        "admin_events",
        """
        writer_generation IS NULL OR
        ((upstream_key_id IS NULL) = (upstream_key_fingerprint IS NULL))
        """,
        postgresql_not_valid=True,
    )

    op.create_unique_constraint(
        "uq_attempt_receipt_started_terminal",
        "upstream_attempt_receipts",
        ["started_event_id", "terminal_event_id"],
    )
    op.create_check_constraint(
        "ck_attempt_receipt_distinct_events",
        "upstream_attempt_receipts",
        "started_event_id <> terminal_event_id",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        "ck_attempt_receipt_terminal_order",
        "upstream_attempt_receipts",
        "terminal_committed_at IS NULL OR terminal_committed_at >= started_at",
        postgresql_not_valid=True,
    )
    op.create_foreign_key(
        "fk_admin_event_exact_attempt_terminal",
        "admin_events",
        "upstream_attempt_receipts",
        ["attempt_started_event_id", "id"],
        ["started_event_id", "terminal_event_id"],
        ondelete="RESTRICT",
        postgresql_not_valid=True,
    )
    _validate_constraint("upstream_attempt_receipts", "ck_attempt_receipt_distinct_events")
    _validate_constraint("upstream_attempt_receipts", "ck_attempt_receipt_terminal_order")
    _validate_constraint("admin_events", "fk_admin_event_exact_attempt_terminal")

    op.create_index(
        "ix_admin_events_request_type",
        "admin_events",
        ["request_id", "event_type", "occurred_at", "id"],
    )
    op.create_index(
        "ix_admin_events_upstream_key",
        "admin_events",
        ["upstream_key_id", "id"],
    )
    op.create_index(
        "ix_attempt_receipts_terminal_age",
        "upstream_attempt_receipts",
        ["terminal_committed_at", "started_event_id"],
    )
    op.create_index(
        "ix_attempt_receipts_request",
        "upstream_attempt_receipts",
        ["request_id", "terminal_committed_at", "started_event_id"],
    )

    _ = op.create_table(
        "admin_ledger_state",
        sa.Column("singleton_id", sa.SmallInteger(), nullable=False),
        sa.Column("rolled_up_routed_request_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "last_maintenance_completed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("last_pruned_event_rows", sa.BigInteger(), nullable=False),
        sa.Column("last_pruned_attempt_rows", sa.BigInteger(), nullable=False),
        sa.Column("last_capacity_blocker", sa.String(length=32), nullable=False),
        sa.CheckConstraint("singleton_id = 1", name="ck_admin_ledger_singleton"),
        sa.CheckConstraint(
            "rolled_up_routed_request_count >= 0",
            name="ck_admin_ledger_rolled_up_count",
        ),
        sa.CheckConstraint(
            "last_pruned_event_rows >= 0",
            name="ck_admin_ledger_pruned_events",
        ),
        sa.CheckConstraint(
            "last_pruned_attempt_rows >= 0",
            name="ck_admin_ledger_pruned_attempts",
        ),
        sa.CheckConstraint(
            """
            last_capacity_blocker IN (
                'none', 'active_attempts', 'reconciliation_grace',
                'lock_contention', 'orphaned_pending', 'legacy_unlinked'
            )
            """,
            name="ck_admin_ledger_capacity_blocker",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO admin_ledger_state (
                singleton_id,
                rolled_up_routed_request_count,
                last_maintenance_completed_at,
                last_pruned_event_rows,
                last_pruned_attempt_rows,
                last_capacity_blocker
            ) VALUES (1, 0, CURRENT_TIMESTAMP, 0, 0, 'none')
            """
        )
    )


def downgrade() -> None:
    """Downgrade only when no rolled-up count or deleted-key handle would be lost."""
    connection = op.get_bind()
    rolled_up = connection.scalar(
        sa.select(_LEDGER_ROLLED_UP).select_from(_LEDGER_TABLE).where(_LEDGER_SINGLETON == 1)
    )
    if rolled_up != 0:
        raise RuntimeError(_ROLLUP_LOSS)
    deleted_key_handle_count = connection.scalar(
        sa.select(sa.func.count())
        .select_from(
            _EVENT_TABLE.outerjoin(
                _UPSTREAM_KEY_TABLE,
                _UPSTREAM_KEY_ID == _EVENT_KEY_ID,
            )
        )
        .where(
            _EVENT_FINGERPRINT.is_not(None),
            _EVENT_KEY_ID.is_not(None),
            _UPSTREAM_KEY_ID.is_(None),
        )
    )
    if deleted_key_handle_count != 0:
        raise RuntimeError(_FINGERPRINT_LOSS)

    op.drop_table("admin_ledger_state")
    op.drop_index("ix_attempt_receipts_request", table_name="upstream_attempt_receipts")
    op.drop_index("ix_attempt_receipts_terminal_age", table_name="upstream_attempt_receipts")
    op.drop_index("ix_admin_events_upstream_key", table_name="admin_events")
    op.drop_index("ix_admin_events_request_type", table_name="admin_events")
    op.drop_constraint(
        "fk_admin_event_exact_attempt_terminal",
        "admin_events",
        type_="foreignkey",
    )
    op.drop_constraint(
        "ck_attempt_receipt_terminal_order",
        "upstream_attempt_receipts",
        type_="check",
    )
    op.drop_constraint(
        "ck_attempt_receipt_distinct_events",
        "upstream_attempt_receipts",
        type_="check",
    )
    op.drop_constraint(
        "uq_attempt_receipt_started_terminal",
        "upstream_attempt_receipts",
        type_="unique",
    )
    op.drop_constraint("ck_admin_event_fingerprint_pair", "admin_events", type_="check")
    op.drop_constraint("ck_admin_event_writer_generation", "admin_events", type_="check")
    op.drop_column("admin_events", "writer_generation")
    op.drop_column("admin_events", "upstream_key_fingerprint")


def _bounded_preflight() -> None:
    connection = op.get_bind()
    limits = (
        (
            sa.select(sa.func.count()).select_from(_EVENT_TABLE),
            _MAX_EVENTS,
            _EVENT_LIMIT,
        ),
        (
            sa.select(sa.func.count()).select_from(_RECEIPT_TABLE),
            _MAX_RECEIPTS,
            _RECEIPT_LIMIT,
        ),
        (
            sa.select(sa.func.count()).select_from(_LIVE_PIN_TABLE),
            _MAX_LIVE_PINS,
            _PIN_LIMIT,
        ),
    )
    for statement, maximum, error_code in limits:
        count = connection.scalar(statement)
        if count is None or count > maximum:
            raise RuntimeError(error_code)


def _validate_constraint(table_name: str, constraint_name: str) -> None:
    op.execute(sa.text(f"ALTER TABLE {table_name} VALIDATE CONSTRAINT {constraint_name}"))
