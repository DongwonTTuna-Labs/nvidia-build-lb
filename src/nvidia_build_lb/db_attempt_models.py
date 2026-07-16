"""Durable attempt, event, and live-pin SQLAlchemy mappings."""

from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from nvidia_build_lb.db import Base
from nvidia_build_lb.db_credential_models import utc_now

EVENT_WRITER_GENERATION: Final = 5


class AdminEventRow(Base):
    """Secret-free append-only administration and attempt event."""

    __tablename__: str = "admin_events"
    __table_args__: tuple[CheckConstraint | ForeignKeyConstraint | Index, ...] = (
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_event_latency"),
        CheckConstraint(
            "writer_generation IS NOT NULL AND writer_generation = 5",
            name="ck_admin_event_writer_generation",
        ),
        CheckConstraint(
            """
            writer_generation IS NULL OR
            ((upstream_key_id IS NULL) = (upstream_key_fingerprint IS NULL))
            """,
            name="ck_admin_event_fingerprint_pair",
        ),
        ForeignKeyConstraint(
            ["attempt_started_event_id", "id"],
            [
                "upstream_attempt_receipts.started_event_id",
                "upstream_attempt_receipts.terminal_event_id",
            ],
            name="fk_admin_event_exact_attempt_terminal",
            ondelete="RESTRICT",
        ),
        Index("ix_admin_events_recent", text("occurred_at DESC"), text("id DESC")),
        Index("ix_admin_events_request_type", "request_id", "event_type", "occurred_at", "id"),
        Index("ix_admin_events_upstream_key", "upstream_key_id", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[str] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(32))
    upstream_key_id: Mapped[UUID | None] = mapped_column(Uuid)
    upstream_key_fingerprint: Mapped[str | None] = mapped_column(String(64))
    downstream_token_id: Mapped[UUID | None] = mapped_column(Uuid)
    outcome_class: Mapped[str] = mapped_column(String(16))
    status_class: Mapped[str | None] = mapped_column(String(32))
    latency_ms: Mapped[int | None] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    attempt_started_event_id: Mapped[UUID | None] = mapped_column(
        Uuid,
        ForeignKey("upstream_attempt_receipts.started_event_id", ondelete="RESTRICT"),
    )
    writer_generation: Mapped[int | None] = mapped_column(SmallInteger)


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
    __table_args__: tuple[CheckConstraint | Index | UniqueConstraint, ...] = (
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
            "transient_failure_streak >= 0",
            name="ck_attempt_receipt_transient_streak",
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
        CheckConstraint(
            "started_event_id <> terminal_event_id",
            name="ck_attempt_receipt_distinct_events",
        ),
        CheckConstraint(
            "terminal_committed_at IS NULL OR terminal_committed_at >= started_at",
            name="ck_attempt_receipt_terminal_order",
        ),
        UniqueConstraint(
            "started_event_id",
            "terminal_event_id",
            name="uq_attempt_receipt_started_terminal",
        ),
        Index(
            "ix_attempt_receipts_key_started",
            "upstream_key_id",
            "started_at",
            "started_event_id",
        ),
        Index(
            "ix_attempt_receipts_terminal_age",
            "terminal_committed_at",
            "started_event_id",
        ),
        Index(
            "ix_attempt_receipts_request",
            "request_id",
            "terminal_committed_at",
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


from nvidia_build_lb.db_live_pin import (  # noqa: E402 - receipt mapper must exist first.
    UpstreamLivePinRow as _UpstreamLivePinRow,
)

UpstreamLivePinRow = _UpstreamLivePinRow


class AdminLedgerStateRow(Base):
    """Singleton retention, rollup, and capacity assessment state."""

    __tablename__: str = "admin_ledger_state"
    __table_args__: tuple[CheckConstraint, ...] = (
        CheckConstraint("singleton_id = 1", name="ck_admin_ledger_singleton"),
        CheckConstraint(
            "rolled_up_routed_request_count >= 0",
            name="ck_admin_ledger_rolled_up_count",
        ),
        CheckConstraint(
            "last_pruned_event_rows >= 0",
            name="ck_admin_ledger_pruned_events",
        ),
        CheckConstraint(
            "last_pruned_attempt_rows >= 0",
            name="ck_admin_ledger_pruned_attempts",
        ),
        CheckConstraint(
            """
            last_capacity_blocker IN (
                'none', 'active_attempts', 'reconciliation_grace',
                'lock_contention', 'orphaned_pending', 'legacy_unlinked'
            )
            """,
            name="ck_admin_ledger_capacity_blocker",
        ),
    )

    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    rolled_up_routed_request_count: Mapped[int] = mapped_column(BigInteger, default=0)
    last_maintenance_completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_pruned_event_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    last_pruned_attempt_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    last_capacity_blocker: Mapped[str] = mapped_column(String(32), default="none")


_ADMIN_EVENT_TERMINAL_INDEX: Final[Index] = Index(
    "uq_admin_events_attempt_terminal",
    AdminEventRow.attempt_started_event_id,
    unique=True,
    postgresql_where=text("attempt_started_event_id IS NOT NULL"),
)
