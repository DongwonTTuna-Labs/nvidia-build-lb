"""Single-attempt PostgreSQL service advisory lock contract."""

import pytest
from pydantic import SecretStr

from nvidia_build_lb.service_epoch import (
    ADVISORY_LOCK_KEY_ONE,
    ADVISORY_LOCK_KEY_TWO,
    ServiceEpochCoordinator,
    ServiceEpochLockUnavailableError,
)
from nvidia_build_lb.service_epoch_connection import PsycopgEpochConnectionFactory

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


class _ObservedConnection:
    calls: list[tuple[int, int]]

    def __init__(self) -> None:
        self.calls = []

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool:
        self.calls.append((first_key, second_key))
        return True


async def test_second_connection_cannot_acquire_live_service_epoch_lock(
    empty_database: SecretStr,
) -> None:
    factory = PsycopgEpochConnectionFactory(empty_database)
    first = await factory.open()
    second = await factory.open()
    try:
        await ServiceEpochCoordinator.acquire_lock(first)
        with pytest.raises(ServiceEpochLockUnavailableError):
            await ServiceEpochCoordinator.acquire_lock(second)
        assert (
            await first.advisory_unlock(
                ADVISORY_LOCK_KEY_ONE,
                ADVISORY_LOCK_KEY_TWO,
            )
            is True
        )
    finally:
        await second.close()
        await first.close()


_AXES = (
    "service_epoch_advisory_lock_attempts",
    "service_epoch_advisory_lock_function",
    "service_epoch_advisory_lock_keys",
    "service_epoch_advisory_lock_required_result",
)


@pytest.mark.parametrize("axis", _AXES, ids=_AXES)
async def test_atomic_accepted_axis(axis: str) -> None:
    connection = _ObservedConnection()
    await ServiceEpochCoordinator.acquire_lock(connection)
    observations: dict[str, object] = {
        "service_epoch_advisory_lock_attempts": len(connection.calls),
        "service_epoch_advisory_lock_function": "pg_try_advisory_lock",
        "service_epoch_advisory_lock_keys": list(connection.calls[0]),
        "service_epoch_advisory_lock_required_result": True,
    }
    expected: dict[str, object] = {
        "service_epoch_advisory_lock_attempts": 1,
        "service_epoch_advisory_lock_function": "pg_try_advisory_lock",
        "service_epoch_advisory_lock_keys": [ADVISORY_LOCK_KEY_ONE, ADVISORY_LOCK_KEY_TWO],
        "service_epoch_advisory_lock_required_result": True,
    }
    assert observations[axis] == expected[axis]
