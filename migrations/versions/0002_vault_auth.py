"""Add encrypted credential, token, event, and scheduler persistence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_vault_auth"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the complete Todo 2 PostgreSQL domain schema."""
    _ = op.create_table(
        "upstream_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("vault_version", sa.SmallInteger(), nullable=False),
        sa.Column("vault_nonce", sa.LargeBinary(), nullable=False),
        sa.Column("vault_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("health_state", sa.String(length=16), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined", sa.Boolean(), nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("success_count", sa.BigInteger(), nullable=False),
        sa.Column("failure_count", sa.BigInteger(), nullable=False),
        sa.Column("consecutive_rate_limits", sa.BigInteger(), nullable=False),
        sa.Column("consecutive_transient_failures", sa.BigInteger(), nullable=False),
        sa.Column("last_status_class", sa.String(length=32), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("vault_version = 1", name="ck_upstream_vault_version"),
        sa.CheckConstraint("octet_length(vault_nonce) = 12", name="ck_upstream_nonce_length"),
        sa.CheckConstraint(
            "octet_length(vault_ciphertext) >= 17",
            name="ck_upstream_ciphertext_length",
        ),
        sa.CheckConstraint("request_count >= 0", name="ck_upstream_request_count"),
        sa.CheckConstraint("success_count >= 0", name="ck_upstream_success_count"),
        sa.CheckConstraint("failure_count >= 0", name="ck_upstream_failure_count"),
        sa.CheckConstraint(
            "consecutive_rate_limits >= 0",
            name="ck_upstream_rate_limit_count",
        ),
        sa.CheckConstraint(
            "consecutive_transient_failures >= 0",
            name="ck_upstream_transient_count",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_upstream_fingerprint"),
    )
    op.create_index(
        "ix_upstream_eligibility",
        "upstream_keys",
        ["enabled", "quarantined", "cooldown_until", "created_at", "id"],
    )
    _ = op.create_table(
        "downstream_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("label_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("token_digest", sa.LargeBinary(), nullable=False),
        sa.Column("models_read", sa.Boolean(), nullable=False),
        sa.Column("chat_write", sa.Boolean(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("octet_length(label_bytes) >= 1", name="ck_downstream_label_bytes"),
        sa.CheckConstraint("octet_length(token_digest) = 32", name="ck_downstream_digest_length"),
        sa.CheckConstraint("models_read OR chat_write", name="ck_downstream_nonempty_scope"),
        sa.CheckConstraint("request_count >= 0", name="ck_downstream_request_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("label_bytes", name="uq_downstream_label_bytes"),
        sa.UniqueConstraint("token_digest", name="uq_downstream_token_digest"),
    )
    _ = op.create_table(
        "scheduler_state",
        sa.Column("singleton_id", sa.SmallInteger(), nullable=False),
        sa.Column("cursor_key_id", sa.Uuid(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("singleton_id = 1", name="ck_scheduler_singleton"),
        sa.ForeignKeyConstraint(
            ["cursor_key_id"],
            ["upstream_keys.id"],
            name="fk_scheduler_cursor_key",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
    )
    op.execute(sa.text("INSERT INTO scheduler_state (singleton_id) VALUES (1)"))
    _ = op.create_table(
        "admin_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("upstream_key_id", sa.Uuid(), nullable=True),
        sa.Column("downstream_token_id", sa.Uuid(), nullable=True),
        sa.Column("outcome_class", sa.String(length=16), nullable=False),
        sa.Column("status_class", sa.String(length=32), nullable=True),
        sa.Column("latency_ms", sa.BigInteger(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_event_latency"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_events_recent",
        "admin_events",
        [sa.text("occurred_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    """Remove only the Todo 2 domain schema."""
    op.drop_index("ix_admin_events_recent", table_name="admin_events")
    op.drop_table("admin_events")
    op.drop_table("scheduler_state")
    op.drop_table("downstream_tokens")
    op.drop_index("ix_upstream_eligibility", table_name="upstream_keys")
    op.drop_table("upstream_keys")
