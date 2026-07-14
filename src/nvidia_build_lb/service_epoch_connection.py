"""Dedicated psycopg async connection for one process service epoch."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar, final
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import tuple_row
from pydantic import SecretStr

from nvidia_build_lb.service_epoch_types import EpochConnectionUnexpectedCloseError

_LOCK_SQL = "SELECT pg_try_advisory_lock(%s, %s)"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(%s, %s)"
PING_SQL = "SELECT 1"
_DELETE_PRIOR_PINS_SQL = (
    "DELETE FROM upstream_live_pins WHERE service_epoch <> %s RETURNING started_event_id"
)
_INVALID_DATABASE_URL = "invalid_database_url"


@dataclass(frozen=True, slots=True)
class PsycopgEpochConnectionFactory:
    """Open exactly one non-pooled psycopg async connection."""

    database_url: SecretStr

    async def open(self) -> "PsycopgEpochConnection":
        """Open with autocommit before any advisory-lock statement."""
        database_url = normalize_psycopg_url(self.database_url.get_secret_value())
        connection = await AsyncConnection.connect(
            database_url,
            autocommit=True,
            row_factory=tuple_row,
        )
        return PsycopgEpochConnection(connection)


@final
class PsycopgEpochConnection:
    """Hide the concrete connection behind a narrow no-pool surface."""

    __slots__: ClassVar[tuple[str, ...]] = ("_connection",)
    _connection: AsyncConnection[tuple[object, ...]]

    def __init__(self, connection: AsyncConnection[tuple[object, ...]]) -> None:
        """Take exclusive ownership of one already-open connection."""
        self._connection = connection

    @property
    def autocommit(self) -> bool:
        """Return the concrete connection autocommit setting."""
        return self._connection.autocommit

    @property
    def dedicated(self) -> bool:
        """Identify this connection as non-pooled."""
        return True

    @property
    def driver(self) -> str:
        """Return the fixed safe driver name."""
        return "psycopg_async"

    @property
    def pool(self) -> str:
        """Return the fixed absence-of-pool marker."""
        return "none"

    @property
    def backend_pid(self) -> int:
        """Expose only the safe PostgreSQL backend PID to the external watchdog."""
        return self._connection.info.backend_pid

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool | None:
        """Attempt the exact two-key session lock once."""
        cursor = await self._connection.execute(_LOCK_SQL, (first_key, second_key))
        return _single_bool(await cursor.fetchone())

    async def delete_prior_epoch_pins(self, active_epoch: UUID) -> int:
        """Commit one same-connection transaction deleting only prior pins."""
        async with self._connection.transaction():
            cursor = await self._connection.execute(_DELETE_PRIOR_PINS_SQL, (active_epoch,))
            rows = await cursor.fetchall()
        return len(rows)

    async def ping(self) -> bool:
        """Return true only for the exact one-row liveness result."""

        async def execute_ping() -> tuple[object, ...] | None:
            cursor = await self._connection.execute(PING_SQL)
            return await cursor.fetchone()

        return await ping_epoch_connection(
            lambda: self._connection.closed,
            execute_ping,
        )

    async def advisory_unlock(self, first_key: int, second_key: int) -> bool | None:
        """Release the exact session lock once."""
        cursor = await self._connection.execute(_UNLOCK_SQL, (first_key, second_key))
        return _single_bool(await cursor.fetchone())

    async def close(self) -> None:
        """Close the owned connection."""
        await self._connection.close()


def _single_bool(row: tuple[object, ...] | None) -> bool | None:
    if row is None or len(row) != 1:
        return None
    value = row[0]
    return value if isinstance(value, bool) else None


async def ping_epoch_connection(
    is_closed: Callable[[], bool],
    execute: Callable[[], Awaitable[tuple[object, ...] | None]],
) -> bool:
    """Classify closure before, during, or immediately after one exact ping."""
    if is_closed():
        raise EpochConnectionUnexpectedCloseError
    row: tuple[object, ...] | None = None
    closed_during_error = False
    try:
        row = await execute()
    except Exception:
        if not is_closed():
            raise
        closed_during_error = True
    if closed_during_error or is_closed():
        raise EpochConnectionUnexpectedCloseError
    return row == (1,)


def normalize_psycopg_url(value: str) -> str:
    """Normalize the two supported SQLAlchemy and psycopg URL schemes."""
    if value.startswith("postgresql+asyncpg://"):
        return "postgresql://" + value.removeprefix("postgresql+asyncpg://")
    if value.startswith(("postgresql://", "postgres://")):
        return value
    raise ValueError(_INVALID_DATABASE_URL)
