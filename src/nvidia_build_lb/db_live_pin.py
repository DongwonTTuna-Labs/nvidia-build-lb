"""Live attempt pin mapping isolated from the primary credential models."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from nvidia_build_lb.db import Base


class UpstreamLivePinRow(Base):
    """Prevent deletion while a process epoch owns an unfinished attempt."""

    __tablename__: str = "upstream_live_pins"
    __table_args__: tuple[Index, ...] = (
        Index("ix_live_pins_key", "upstream_key_id", "started_event_id"),
        Index("ix_live_pins_epoch", "service_epoch", "started_event_id"),
    )

    started_event_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("upstream_attempt_receipts.started_event_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    upstream_key_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("upstream_keys.id", ondelete="RESTRICT"),
    )
    service_epoch: Mapped[UUID] = mapped_column(Uuid)
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
