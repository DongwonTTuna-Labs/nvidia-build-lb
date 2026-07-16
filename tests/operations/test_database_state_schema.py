"""Fail-closed database-state dispatch sensors for partial 0005 shapes."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts/ops/database-state.sh"
_CONTAINER = "a" * 64


@pytest.mark.parametrize(
    ("revision", "exact", "absent"),
    [
        ("0005_admin_dashboard_ledger", "0", "0"),
        ("0005_admin_dashboard_ledger", "0", "1"),
        ("0004_vault_key_verifier", "1", "0"),
    ],
)
def test_partial_or_revision_mismatched_schema_fails_before_state_projection(
    tmp_path: Path,
    revision: str,
    exact: str,
    absent: str,
) -> None:
    binary = tmp_path / "docker"
    log = tmp_path / "docker.log"
    _ = binary.write_text(
        """#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_LOG"
if [ "$1" = inspect ]; then
  case "$3" in
    *State.Running*) printf '%s\n' true ;;
    *nvidia-build-lb.component*) printf '%s\n' database ;;
    *com.docker.compose.service*) printf '%s\n' db ;;
    *com.docker.compose.project*) printf '%s\n' schema-test ;;
    *) exit 99 ;;
  esac
  exit 0
fi
case "$*" in
  *"SELECT version_num FROM alembic_version"*) printf '%s\n' "$REVISION" ;;
  *"WITH expected_columns"*) printf '%s\t%s\n' "$V3_EXACT" "$V3_ABSENT" ;;
  *) exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    environment = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "REVISION": revision,
        "V3_EXACT": exact,
        "V3_ABSENT": absent,
    }

    completed = subprocess.run(  # noqa: S603 - fixed repository script boundary.
        [_SCRIPT, _CONTAINER],
        cwd=_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "database_state_failed\n"
    assert "vault_key_verifier" not in log.read_text(encoding="utf-8")
