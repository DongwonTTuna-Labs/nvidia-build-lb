"""Bind the configured vault key to the encrypted database state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_vault_key_verifier"
down_revision: str | None = "0003_nvidia_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one initially-unbound verifier row for application initialization."""
    _ = op.create_table(
        "vault_key_verifier",
        sa.Column("singleton_id", sa.SmallInteger(), nullable=False),
        sa.Column("verifier_salt", sa.LargeBinary(), nullable=True),
        sa.Column("verifier_digest", sa.LargeBinary(), nullable=True),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("singleton_id = 1", name="ck_vault_verifier_singleton"),
        sa.CheckConstraint(
            """
            (
                verifier_salt IS NULL
                AND verifier_digest IS NULL
                AND initialized_at IS NULL
            )
            OR (
                octet_length(verifier_salt) = 32
                AND octet_length(verifier_digest) = 32
                AND initialized_at IS NOT NULL
            )
            """,
            name="ck_vault_verifier_state",
        ),
        sa.PrimaryKeyConstraint("singleton_id"),
    )
    op.execute(sa.text("INSERT INTO vault_key_verifier (singleton_id) VALUES (1)"))


def downgrade() -> None:
    """Remove only the vault-key binding row."""
    op.drop_table("vault_key_verifier")
