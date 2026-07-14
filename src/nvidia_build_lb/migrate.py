"""Secret-safe one-shot Alembic runner used by the candidate compose."""

import sys

from alembic import command
from alembic.config import Config

from nvidia_build_lb.config import load_database_url


def run() -> None:
    """Upgrade the configured database to the repository head."""
    database_url = load_database_url().get_secret_value().replace("%", "%%")
    config = Config("/app/alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def main() -> None:
    """Return one fixed failure diagnostic without rendering the DSN."""
    status = 0
    try:
        run()
    except BaseException:  # noqa: BLE001 - process boundary emits only a fixed code.
        status = 1
        _ = sys.stderr.write("migration_failed\n")
    raise SystemExit(status)


if __name__ == "__main__":
    main()
