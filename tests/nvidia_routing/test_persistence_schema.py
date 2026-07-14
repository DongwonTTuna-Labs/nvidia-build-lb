"""Physical 0003 routing persistence schema surface."""

import pytest

from nvidia_build_lb import db_models
from nvidia_build_lb.db import Base

pytestmark = pytest.mark.nvidia_routing


def test_routing_tables_share_the_application_metadata() -> None:
    tables = Base.metadata.tables
    assert db_models.UpstreamKeyRow.metadata is Base.metadata
    assert "upstream_attempt_receipts" in tables
    assert "upstream_live_pins" in tables
    assert "attempt_started_event_id" in tables["admin_events"].columns
    assert "cooldown_kind" in tables["upstream_keys"].columns
