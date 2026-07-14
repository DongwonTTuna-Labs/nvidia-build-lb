"""Recompute and verify the pinned runtime closure from installed bytes."""

import ast
import base64
import csv
import hashlib
import importlib.metadata
import sys
from io import StringIO
from pathlib import Path

from nvidia_build_lb.runtime_manifest_data import (
    APPROVED_CLOSURE,
    EXPECTED_CLOSURE_MERKLE,
    EXPECTED_DISTRIBUTIONS,
    EXPECTED_LOGGER_NAMES,
    FORBIDDEN_MODULE_PREFIXES,
    NON_PROVIDER_HASHES,
)
from nvidia_build_lb.runtime_types import PinnedRuntimeDriftError, RuntimeReceipt

_RECORD_FIELD_COUNT = 3


def verify_runtime_closure() -> RuntimeReceipt:
    """Recompute versions, RECORDs, package Merkle roots, and sentinels."""
    roots: dict[str, Path] = {}
    record_rows: dict[str, tuple[str, str]] = {}
    source_count = 0
    for package, expected in EXPECTED_DISTRIBUTIONS.items():
        distribution = importlib.metadata.distribution(package)
        if distribution.version != expected[0]:
            raise PinnedRuntimeDriftError
        root = Path(str(distribution.locate_file(""))).resolve()
        roots[package] = root
        files = distribution.files
        if files is None:
            raise PinnedRuntimeDriftError
        record_candidates = tuple(
            path for path in files if path.as_posix().endswith(".dist-info/RECORD")
        )
        if len(record_candidates) != 1:
            raise PinnedRuntimeDriftError
        record_bytes = Path(str(distribution.locate_file(record_candidates[0]))).read_bytes()
        if stable_record_digest(record_bytes) != expected[1]:
            raise PinnedRuntimeDriftError
        package_rows = _record_rows(record_bytes)
        record_rows.update(package_rows)
        sources = sorted(
            path.as_posix()
            for path in files
            if path.as_posix().startswith(f"{package}/")
            and path.suffix == ".py"
            and "__pycache__" not in path.parts
        )
        entries = tuple(_source_entry(relative, root, package_rows) for relative in sources)
        if len(entries) != expected[2] or _merkle(entries) != expected[3]:
            raise PinnedRuntimeDriftError
        source_count += len(entries)

    closure_entries = tuple(
        _source_entry(relative, roots[relative.split("/", 1)[0]], record_rows)
        for relative in sorted(APPROVED_CLOSURE)
    )
    if _merkle(closure_entries) != EXPECTED_CLOSURE_MERKLE:
        raise PinnedRuntimeDriftError
    if _extract_logger_names(closure_entries, roots) != EXPECTED_LOGGER_NAMES:
        raise PinnedRuntimeDriftError
    _verify_non_provider_hashes()
    if forbidden_loaded_modules():
        raise PinnedRuntimeDriftError
    return RuntimeReceipt(
        distribution_count=len(EXPECTED_DISTRIBUTIONS),
        python_source_count=source_count,
        closure_count=len(closure_entries),
        closure_merkle_sha256=EXPECTED_CLOSURE_MERKLE,
        logger_count=len(EXPECTED_LOGGER_NAMES),
        non_provider_hash_count=len(NON_PROVIDER_HASHES),
    )


def forbidden_loaded_modules() -> tuple[str, ...]:
    """Return forbidden normal-provider modules already present."""
    return tuple(
        name
        for name in sys.modules
        if any(
            name == prefix or name.startswith(f"{prefix}.") for prefix in FORBIDDEN_MODULE_PREFIXES
        )
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_rows(record_bytes: bytes) -> dict[str, tuple[str, str]]:
    text = record_bytes.decode("utf-8", errors="strict")
    result: dict[str, tuple[str, str]] = {}
    for row in csv.reader(StringIO(text)):
        if len(row) == _RECORD_FIELD_COUNT and row[1] and row[2]:
            result[row[0]] = (row[1], row[2])
    return result


def stable_record_digest(record_bytes: bytes) -> str:
    """Hash canonical distribution-local RECORD rows, excluding generated launchers."""
    text = record_bytes.decode("utf-8", errors="strict")
    rows = tuple(csv.reader(StringIO(text)))
    if any(len(row) != _RECORD_FIELD_COUNT for row in rows):
        raise PinnedRuntimeDriftError
    canonical = StringIO(newline="")
    writer = csv.writer(canonical, lineterminator="\n")
    writer.writerows(row for row in rows if not row[0].startswith("../"))
    return _digest(canonical.getvalue().encode("utf-8"))


def _source_entry(
    relative: str,
    root: Path,
    record_rows: dict[str, tuple[str, str]],
) -> tuple[str, str]:
    path = (root / relative).resolve()
    if root not in path.parents or path.is_symlink():
        raise PinnedRuntimeDriftError
    data = path.read_bytes()
    current = _digest(data)
    record = record_rows.get(relative)
    if record is None:
        raise PinnedRuntimeDriftError
    algorithm, encoded = record[0].split("=", 1)
    expected_digest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
    if algorithm != "sha256" or expected_digest != current or int(record[1]) != len(data):
        raise PinnedRuntimeDriftError
    return relative, current


def _merkle(entries: tuple[tuple[str, str], ...]) -> str:
    payload = b"".join(
        path.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n" for path, digest in entries
    )
    return _digest(payload)


def _extract_logger_names(
    entries: tuple[tuple[str, str], ...],
    roots: dict[str, Path],
) -> frozenset[str]:
    names: set[str] = set()
    for relative, _digest_value in entries:
        root = roots[relative.split("/", 1)[0]]
        tree = ast.parse((root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function, argument = node.func, node.args[0]
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "getLogger"
                and isinstance(function.value, ast.Name)
                and function.value.id == "logging"
                and isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
            ):
                names.add(argument.value)
    return frozenset(names)


def _verify_non_provider_hashes() -> None:
    for relative, (distribution_name, expected) in NON_PROVIDER_HASHES.items():
        distribution = importlib.metadata.distribution(distribution_name)
        if _digest(Path(str(distribution.locate_file(relative))).read_bytes()) != expected:
            raise PinnedRuntimeDriftError
