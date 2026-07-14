"""SQLAlchemy mappings for credential and durable scheduling state."""

from datetime import UTC, datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    SmallInteger,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
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
            "octet_length(vault_ciphertext) >= 17", name="ck_upstream_ciphertext_length"
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
        CheckConstraint("consecutive_rate_limits >= 0", name="ck_upstream_rate_limit_count"),
        CheckConstraint("consecutive_transient_failures >= 0", name="ck_upstream_transient_count"),
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


class AdminEventRow(Base):
    """Secret-free append-only administration and attempt event."""

    __tablename__: str = "admin_events"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_event_latency"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(32))
    upstream_key_id: Mapped[UUID | None] = mapped_column(Uuid)
    downstream_token_id: Mapped[UUID | None] = mapped_column(Uuid)
    outcome_class: Mapped[str] = mapped_column(String(16))
    status_class: Mapped[str | None] = mapped_column(String(32))
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    attempt_started_event_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("upstream_attempt_receipts.started_event_id", ondelete="RESTRICT"),
    )


class UpstreamAttemptReceiptRow(Base):
    """Idempotency and crash-gap receipt for one selected upstream attempt."""

    __tablename__: str = "upstream_attempt_receipts"

    started_event_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("admin_events.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    terminal_event_id: Mapped[UUID] = mapped_column(Uuid, unique=True)
    upstream_key_id: Mapped[UUID] = mapped_column(Uuid)
    request_id: Mapped[str] = mapped_column(String(128))
    service_epoch: Mapped[UUID] = mapped_column(Uuid)
    explicit_probe_key_id: Mapped[UUID | None] = mapped_column(Uuid)
    __table_args__: tuple[CheckConstraint | Index, ...] = (
        CheckConstraint(
            """
            (
                (
                    terminal_outcome IS NULL
                    AND terminal_status_class IS NULL
                    AND terminal_latency_ms IS NULL
                    AND terminal_cooldown_until IS NULL
                    AND terminal_cooldown_kind IS NULL
                    AND terminal_committed_at IS NULL
                )
                OR (
                    terminal_outcome IN ('succeeded','failed','cancelled')
                    AND terminal_status_class IS NOT NULL
                    AND terminal_latency_ms >= 0
                    AND terminal_committed_at IS NOT NULL
                    AND (
                        (terminal_cooldown_until IS NULL AND terminal_cooldown_kind IS NULL)
                        OR (
                            terminal_cooldown_until IS NOT NULL
                            AND terminal_cooldown_kind IS NOT NULL
                        )
                    )
                )
            )
            """,
            name="ck_attempt_receipt_state",
        ),
        CheckConstraint(
            """
            terminal_cooldown_kind IS NULL
            OR terminal_cooldown_kind IN ('rate_limit','transient')
            """,
            name="ck_attempt_receipt_cooldown_kind",
        ),
        CheckConstraint("rate_limit_streak >= 0", name="ck_attempt_receipt_rate_streak"),
        CheckConstraint(
            "transient_failure_streak >= 0", name="ck_attempt_receipt_transient_streak"
        ),
        CheckConstraint(
            """
            cardinality(excluded_key_ids) <= 1
            AND array_position(excluded_key_ids, NULL) IS NULL
            """,
            name="ck_attempt_receipt_excluded",
        ),
        CheckConstraint(
            """
            explicit_probe_key_id IS NULL
            OR (
                cardinality(excluded_key_ids) = 0
                AND upstream_key_id = explicit_probe_key_id
            )
            """,
            name="ck_attempt_receipt_selection",
        ),
        Index(
            "ix_attempt_receipts_key_started",
            "upstream_key_id",
            "started_at",
            "started_event_id",
        ),
    )

    excluded_key_ids: Mapped[list[UUID]] = mapped_column(ARRAY(Uuid()), default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rate_limit_streak: Mapped[int] = mapped_column(BigInteger)
    transient_failure_streak: Mapped[int] = mapped_column(BigInteger)
    terminal_outcome: Mapped[str | None] = mapped_column(String(16))
    terminal_status_class: Mapped[str | None] = mapped_column(String(32))
    terminal_latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    terminal_cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminal_cooldown_kind: Mapped[str | None] = mapped_column(String(16))
    terminal_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


from nvidia_build_lb.db_live_pin import (  # noqa: E402 - mapper order enforces receipt first.
    UpstreamLivePinRow as _UpstreamLivePinRow,
)

UpstreamLivePinRow = _UpstreamLivePinRow


_ADMIN_EVENT_TERMINAL_INDEX: Final[Index] = Index(
    "uq_admin_events_attempt_terminal",
    AdminEventRow.attempt_started_event_id,
    unique=True,
    postgresql_where=text("attempt_started_event_id IS NOT NULL"),
)


DOMAIN_METADATA: Final[MetaData] = Base.metadata
