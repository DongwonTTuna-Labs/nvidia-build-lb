"""Dedicated psycopg service-epoch connection contract."""

import pytest
from pydantic import SecretStr

from nvidia_build_lb.service_epoch_connection import (
    PsycopgEpochConnectionFactory,
    ping_epoch_connection,
)
from nvidia_build_lb.service_epoch_types import EpochConnectionUnexpectedCloseError

pytestmark = [pytest.mark.nvidia_routing, pytest.mark.anyio]


async def test_dedicated_connection_is_psycopg_async_autocommit_without_pool(
    empty_database: SecretStr,
) -> None:
    connection = await PsycopgEpochConnectionFactory(empty_database).open()
    try:
        assert connection.autocommit is True
        assert connection.dedicated is True
        assert connection.driver == "psycopg_async"
        assert connection.pool == "none"
    finally:
        await connection.close()


async def test_closed_dedicated_connection_raises_typed_unexpected_close(
    empty_database: SecretStr,
) -> None:
    connection = await PsycopgEpochConnectionFactory(empty_database).open()
    await connection.close()

    with pytest.raises(EpochConnectionUnexpectedCloseError):
        _ = await connection.ping()


async def test_ping_closure_during_execute_raises_typed_unexpected_close() -> None:
    closed = False
    driver_error = OSError("driver detail must not escape")

    def is_closed() -> bool:
        return closed

    async def execute() -> tuple[object, ...] | None:
        nonlocal closed
        closed = True
        raise driver_error

    with pytest.raises(EpochConnectionUnexpectedCloseError) as caught:
        _ = await ping_epoch_connection(is_closed, execute)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


async def test_ping_closure_after_fetch_raises_typed_unexpected_close() -> None:
    closed = False

    def is_closed() -> bool:
        return closed

    async def execute() -> tuple[object, ...] | None:
        nonlocal closed
        closed = True
        return (1,)

    with pytest.raises(EpochConnectionUnexpectedCloseError):
        _ = await ping_epoch_connection(is_closed, execute)


async def test_ping_preserves_sql_error_while_connection_remains_open() -> None:
    expected = RuntimeError("safe test SQL failure")

    async def execute() -> tuple[object, ...] | None:
        raise expected

    with pytest.raises(RuntimeError) as caught:
        _ = await ping_epoch_connection(lambda: False, execute)

    assert caught.value is expected


_AXES = (
    "service_epoch_connection_autocommit",
    "service_epoch_connection_dedicated",
    "service_epoch_connection_driver",
    "service_epoch_connection_pool",
)


@pytest.mark.parametrize("axis", _AXES, ids=_AXES)
async def test_atomic_accepted_axis(axis: str, empty_database: SecretStr) -> None:
    connection = await PsycopgEpochConnectionFactory(empty_database).open()
    try:
        observed: dict[str, bool | str] = {
            "service_epoch_connection_autocommit": connection.autocommit,
            "service_epoch_connection_dedicated": connection.dedicated,
            "service_epoch_connection_driver": connection.driver,
            "service_epoch_connection_pool": connection.pool,
        }
        expected: dict[str, bool | str] = {
            "service_epoch_connection_autocommit": True,
            "service_epoch_connection_dedicated": True,
            "service_epoch_connection_driver": "psycopg_async",
            "service_epoch_connection_pool": "none",
        }
        assert observed[axis] == expected[axis]
    finally:
        await connection.close()
