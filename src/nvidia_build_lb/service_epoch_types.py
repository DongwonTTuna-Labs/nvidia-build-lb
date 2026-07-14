"""Typed boundaries shared by service-epoch lifecycle and persistence."""

from dataclasses import dataclass
from typing import Protocol, final, override
from uuid import UUID


@final
class EpochConnectionUnexpectedCloseError(Exception):
    """Signal that the dedicated epoch connection closed outside clean shutdown."""

    __slots__ = ()

    @override
    def __str__(self) -> str:
        """Return only the stable safe classification."""
        return "epoch_connection_unexpected_close"


@dataclass(frozen=True, slots=True)
class PriorEpochCleanup:
    """Proof that only pins outside the newly active epoch were removed."""

    deleted_count: int


class EpochSqlConnection(Protocol):
    """One dedicated, advisory-lock-owning PostgreSQL connection."""

    @property
    def autocommit(self) -> bool:
        """Return whether ordinary statements commit independently."""
        ...

    @property
    def dedicated(self) -> bool:
        """Return true only for a non-pooled connection."""
        ...

    @property
    def driver(self) -> str:
        """Return the fixed safe driver discriminator."""
        ...

    @property
    def pool(self) -> str:
        """Return the fixed safe pool discriminator."""
        ...

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool | None:
        """Attempt one two-key session advisory lock."""
        ...

    async def delete_prior_epoch_pins(self, active_epoch: UUID) -> int:
        """Delete pins outside the active epoch in one same-connection transaction."""
        ...

    async def ping(self) -> bool:
        """Run the fixed liveness query on this exact connection."""
        ...

    async def advisory_unlock(self, first_key: int, second_key: int) -> bool | None:
        """Release the exact session advisory lock once."""
        ...

    async def close(self) -> None:
        """Close this dedicated connection once."""
        ...


class EpochConnectionFactory(Protocol):
    """Open one dedicated autocommit service-epoch connection."""

    async def open(self) -> EpochSqlConnection:
        """Return a fresh non-pooled connection."""
        ...
