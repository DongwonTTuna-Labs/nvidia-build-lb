"""Independent PostgreSQL backend and advisory-lock absence probes."""

from dataclasses import dataclass

from psycopg import AsyncConnection
from psycopg.rows import tuple_row
from pydantic import SecretStr

from nvidia_build_lb.service_epoch_connection import normalize_psycopg_url

_ABSENCE_SQL = """
SELECT
  NOT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid = %s),
  NOT EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory'
      AND classid = %s::oid
      AND objid = %s::oid
      AND objsubid = 2
      AND granted
  )
"""
_ABSENCE_COLUMN_COUNT = 2


@dataclass(frozen=True, slots=True)
class PostgresEpochAbsenceProbe:
    """Query PostgreSQL from a connection independent of the service child."""

    database_url: SecretStr
    backend_pid: int
    first_lock_key: int
    second_lock_key: int

    async def observe(self) -> tuple[bool, bool]:
        """Return exact backend and two-int4 advisory-lock absence."""
        connection = await AsyncConnection.connect(
            normalize_psycopg_url(self.database_url.get_secret_value()),
            autocommit=True,
            row_factory=tuple_row,
        )
        try:
            cursor = await connection.execute(
                _ABSENCE_SQL,
                (self.backend_pid, self.first_lock_key, self.second_lock_key),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if (
            row is None
            or len(row) != _ABSENCE_COLUMN_COUNT
            or not isinstance(row[0], bool)
            or not isinstance(row[1], bool)
        ):
            return False, False
        return row[0], row[1]
