"""Credential, scheduler, and vault-verifier SQLAlchemy mappings."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from nvidia_build_lb.admin.schemas import HealthState
from nvidia_build_lb.db import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp for application-owned writes."""
    return datetime.now(UTC)


class UpstreamKeyRow(Base):
    """Encrypted NVIDIA credential and its routing state."""

    __tablename__: str = "upstream_keys"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("vault_version = 1", name="ck_upstream_vault_version"),
        CheckConstraint("octet_length(vault_nonce) = 12", name="ck_upstream_nonce_length"),
        CheckConstraint(
            "octet_length(vault_ciphertext) >= 17",
            name="ck_upstream_ciphertext_length",
        ),
        CheckConstraint("request_count >= 0", name="ck_upstream_request_count"),
        CheckConstraint("success_count >= 0", name="ck_upstream_success_count"),
        CheckConstraint("failure_count >= 0", name="ck_upstream_failure_count"),
        CheckConstraint(
            "(cooldown_until IS NULL) = (cooldown_kind IS NULL)",
            name="ck_upstream_cooldown_pair",
        ),
        CheckConstraint(
            "cooldown_kind IS NULL OR cooldown_kind IN ('rate_limit','transient')",
            name="ck_upstream_cooldown_kind",
        ),
        CheckConstraint(
            "consecutive_rate_limits >= 0",
            name="ck_upstream_rate_limit_count",
        ),
        CheckConstraint(
            "consecutive_transient_failures >= 0",
            name="ck_upstream_transient_count",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    vault_version: Mapped[int] = mapped_column(SmallInteger, default=1)
    vault_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    vault_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    health_state: Mapped[str] = mapped_column(String(16), default=HealthState.UNKNOWN.value)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cooldown_kind: Mapped[str | None] = mapped_column(String(16))
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    request_count: Mapped[int] = mapped_column(BigInteger, default=0)
    success_count: Mapped[int] = mapped_column(BigInteger, default=0)
    failure_count: Mapped[int] = mapped_column(BigInteger, default=0)
    consecutive_rate_limits: Mapped[int] = mapped_column(BigInteger, default=0)
    consecutive_transient_failures: Mapped[int] = mapped_column(BigInteger, default=0)
    last_status_class: Mapped[str | None] = mapped_column(String(32))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DownstreamTokenRow(Base):
    """Digest-only downstream bearer and its exact permissions."""

    __tablename__: str = "downstream_tokens"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("octet_length(label_bytes) >= 1", name="ck_downstream_label_bytes"),
        CheckConstraint("octet_length(token_digest) = 32", name="ck_downstream_digest_length"),
        CheckConstraint("models_read OR chat_write", name="ck_downstream_nonempty_scope"),
        CheckConstraint("request_count >= 0", name="ck_downstream_request_count"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(Text)
    label_bytes: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    token_digest: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    models_read: Mapped[bool] = mapped_column(Boolean)
    chat_write: Mapped[bool] = mapped_column(Boolean)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(BigInteger, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SchedulerStateRow(Base):
    """Singleton lock row for the persisted round-robin cursor."""

    __tablename__: str = "scheduler_state"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("singleton_id = 1", name="ck_scheduler_singleton"),
    )

    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    cursor_key_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("upstream_keys.id", ondelete="SET NULL"),
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VaultKeyVerifierRow(Base):
    """Singleton HMAC verifier binding one master key to this database."""

    __tablename__: str = "vault_key_verifier"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("singleton_id = 1", name="ck_vault_verifier_singleton"),
        CheckConstraint(
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
    )

    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    verifier_salt: Mapped[bytes | None] = mapped_column(LargeBinary)
    verifier_digest: Mapped[bytes | None] = mapped_column(LargeBinary)
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
