"""Fail-closed metadata validation for a service-epoch connection."""

from nvidia_build_lb.service_epoch_types import EpochSqlConnection


def is_valid_epoch_connection(connection: EpochSqlConnection) -> bool:
    """Require the exact dedicated async psycopg connection contract."""
    try:
        autocommit = connection.autocommit
        dedicated = connection.dedicated
        driver = connection.driver
        pool = connection.pool
        return (
            type(autocommit) is bool
            and autocommit is True
            and type(dedicated) is bool
            and dedicated is True
            and type(driver) is str
            and driver == "psycopg_async"
            and type(pool) is str
            and pool == "none"
        )
    except Exception:  # noqa: BLE001 - untrusted metadata is only invalid-open.
        return False
