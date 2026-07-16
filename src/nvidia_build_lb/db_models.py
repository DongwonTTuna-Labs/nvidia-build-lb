"""Stable import facade for all SQLAlchemy domain mappings."""

from typing import Final

from sqlalchemy import MetaData

from nvidia_build_lb.db import Base
from nvidia_build_lb.db_attempt_models import (
    EVENT_WRITER_GENERATION,
    AdminEventRow,
    AdminLedgerStateRow,
    UpstreamAttemptReceiptRow,
    UpstreamLivePinRow,
)
from nvidia_build_lb.db_credential_models import (
    DownstreamTokenRow,
    SchedulerStateRow,
    UpstreamKeyRow,
    VaultKeyVerifierRow,
    utc_now,
)

__all__ = [
    "DOMAIN_METADATA",
    "EVENT_WRITER_GENERATION",
    "AdminEventRow",
    "AdminLedgerStateRow",
    "DownstreamTokenRow",
    "SchedulerStateRow",
    "UpstreamAttemptReceiptRow",
    "UpstreamKeyRow",
    "UpstreamLivePinRow",
    "VaultKeyVerifierRow",
    "utc_now",
]

DOMAIN_METADATA: Final[MetaData] = Base.metadata
