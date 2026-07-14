"""Materialize and freeze one manifest-bound QA source snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from typing import ClassVar, Literal, final

from pydantic import BaseModel, ConfigDict

from scripts.qa.source_manifest import CandidatePayload, build_manifest, read_candidate_entry

_MODE_DIGITS = 4
_SHA256_HEX_LENGTH = 64


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class SnapshotEntry(_StrictModel):
    """One manifest entry accepted for snapshot materialization."""

    mode: str
    path: str
    payload_sha256: str
    type: Literal["regular", "symlink"]


class SnapshotManifest(_StrictModel):
    """The exact source-manifest schema consumed by the snapshotter."""

    algorithm: Literal["git-files-type-mode-path-payload-sha256-v1"]
    entries: tuple[SnapshotEntry, ...]
    entry_count: int
    schema_version: Literal[1]
    source_tree_sha256: str


class SnapshotReceipt(_StrictModel):
    """Secret-free proof that a read-only snapshot was materialized."""

    entry_count: int
    regular_files: int
    schema_version: Literal[1] = 1
    source_tree_sha256: str
    status: Literal["PASS"] = "PASS"
    symlinks: int
    writable_regular_files: Literal[0] = 0


@final
class _Arguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.manifest = Path()
        self.output = Path()
        self.receipt = Path()
        self.root = Path.cwd()


def _relative_path(value: str) -> Path:
    posix = PurePosixPath(value)
    if (
        posix.is_absolute()
        or not posix.parts
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        reason = "snapshot manifest path is not a closed relative path"
        raise ValueError(reason)
    return Path(*posix.parts)


def _safe_link_target(relative: Path, target: str) -> None:
    posix = PurePosixPath(target)
    if posix.is_absolute():
        reason = "snapshot symlink target is absolute"
        raise ValueError(reason)
    normalized = PurePosixPath(os.path.normpath(str(PurePosixPath(*relative.parent.parts) / posix)))
    if normalized == PurePosixPath("..") or normalized.parts[:1] == ("..",):
        reason = "snapshot symlink target escapes the source root"
        raise ValueError(reason)


def _assert_snapshot_entry(root: Path, entry: SnapshotEntry, *, frozen: bool) -> None:
    payload = read_candidate_entry(root, entry.path)
    observed_digest = hashlib.sha256(payload.payload).hexdigest()
    expected_mode = int(entry.mode, 8) & (~0o222 if frozen and entry.type == "regular" else 0o777)
    if (
        payload.kind != entry.type
        or observed_digest != entry.payload_sha256
        or int(payload.mode, 8) != expected_mode
    ):
        reason = "snapshot type, mode, or payload changed during materialization"
        raise ValueError(reason)


def _validated_entries(manifest: SnapshotManifest) -> tuple[tuple[SnapshotEntry, Path], ...]:
    if manifest.entry_count != len(manifest.entries):
        reason = "source manifest entry count changed"
        raise ValueError(reason)
    if len({entry.path for entry in manifest.entries}) != len(manifest.entries):
        reason = "source manifest contains duplicate paths"
        raise ValueError(reason)
    validated = tuple((entry, _relative_path(entry.path)) for entry in manifest.entries)
    link_paths = {relative for entry, relative in validated if entry.type == "symlink"}
    for entry, relative in validated:
        if len(entry.mode) != _MODE_DIGITS or any(
            character not in "01234567" for character in entry.mode
        ):
            reason = "source manifest contains an invalid mode"
            raise ValueError(reason)
        if len(entry.payload_sha256) != _SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in entry.payload_sha256
        ):
            reason = "source manifest contains an invalid payload digest"
            raise ValueError(reason)
        if any(parent in link_paths for parent in relative.parents):
            reason = "source manifest nests an entry below a symlink"
            raise ValueError(reason)
    return validated


def _assert_candidate_payload(entry: SnapshotEntry, payload: CandidatePayload) -> None:
    digest = hashlib.sha256(payload.payload).hexdigest()
    if payload.kind != entry.type or payload.mode != entry.mode or digest != entry.payload_sha256:
        reason = "source entry changed during snapshot materialization"
        raise ValueError(reason)


def _open_directory_no_follow(path: Path) -> int:
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_snapshot_parent(output: Path, relative: Path) -> tuple[int, str]:
    descriptor = _open_directory_no_follow(output)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        for component in relative.parts[:-1]:
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, relative.parts[-1]


def _write_all(descriptor: int, payload: bytes) -> None:
    written = 0
    while written < len(payload):
        count = os.write(descriptor, payload[written:])
        if count <= 0:
            reason = "snapshot write made no progress"
            raise OSError(reason)
        written += count


def _create_snapshot_leaf(
    output: Path,
    relative: Path,
    entry: SnapshotEntry,
    payload: CandidatePayload,
) -> None:
    parent, name = _open_snapshot_parent(output, relative)
    descriptor = -1
    try:
        if entry.type == "regular":
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            _write_all(descriptor, payload.payload)
            os.fchmod(descriptor, int(entry.mode, 8))
            os.fsync(descriptor)
        else:
            target = os.fsdecode(payload.payload)
            _safe_link_target(relative, target)
            os.symlink(target, name, dir_fd=parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _materialize_entries(
    root: Path,
    output: Path,
    entries: tuple[tuple[SnapshotEntry, Path], ...],
) -> tuple[int, int]:
    regular_files = 0
    symlinks = 0
    for entry, relative in entries:
        payload = read_candidate_entry(root, entry.path)
        _assert_candidate_payload(entry, payload)
        _create_snapshot_leaf(output, relative, entry, payload)
        if entry.type == "regular":
            regular_files += 1
        else:
            symlinks += 1
        _assert_snapshot_entry(output, entry, frozen=False)
    return regular_files, symlinks


def _fchmod_snapshot_leaf(output: Path, relative: Path, mode: int) -> None:
    parent, name = _open_snapshot_parent(output, relative)
    descriptor = -1
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            reason = "snapshot regular file changed before freeze"
            raise ValueError(reason)
        os.fchmod(descriptor, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _fchmod_snapshot_directory(path: Path, mode: int) -> None:
    descriptor = _open_directory_no_follow(path)
    try:
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _expected_tree(
    entries: tuple[tuple[SnapshotEntry, Path], ...],
) -> tuple[set[Path], set[Path]]:
    leaves = {relative for _, relative in entries}
    directories = {
        parent for _, relative in entries for parent in relative.parents if parent != Path()
    }
    return leaves, directories


def _observed_tree(output: Path) -> tuple[set[Path], set[Path]]:
    leaves: set[Path] = set()
    directories: set[Path] = set()

    def visit(relative: Path) -> None:
        with os.scandir(output / relative) as children:
            for child in children:
                child_relative = relative / child.name
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    directories.add(child_relative)
                    visit(child_relative)
                else:
                    leaves.add(child_relative)

    visit(Path())
    return leaves, directories


def _assert_exact_tree(
    output: Path,
    entries: tuple[tuple[SnapshotEntry, Path], ...],
) -> set[Path]:
    expected_leaves, expected_directories = _expected_tree(entries)
    observed_leaves, observed_directories = _observed_tree(output)
    if observed_leaves != expected_leaves or observed_directories != expected_directories:
        reason = "snapshot tree contains a missing or extra path"
        raise ValueError(reason)
    return expected_directories


def _freeze_entries(
    output: Path,
    entries: tuple[tuple[SnapshotEntry, Path], ...],
) -> None:
    directories = _assert_exact_tree(output, entries)
    writable = 0
    for entry, relative in entries:
        if entry.type == "regular":
            _fchmod_snapshot_leaf(output, relative, int(entry.mode, 8) & ~0o222)
            frozen_payload = read_candidate_entry(output, entry.path)
            writable += int(bool(int(frozen_payload.mode, 8) & 0o222))
        _assert_snapshot_entry(output, entry, frozen=True)
    for relative in sorted(
        directories,
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        _fchmod_snapshot_directory(output / relative, 0o555)
    _fchmod_snapshot_directory(output, 0o555)
    if writable != 0:
        reason = "snapshot retained a writable regular file"
        raise ValueError(reason)
    _ = _assert_exact_tree(output, entries)


def _remove_partial_snapshot(output: Path) -> None:
    for current, _, _ in os.walk(output, topdown=False, followlinks=False):
        _fchmod_snapshot_directory(Path(current), 0o700)
    shutil.rmtree(output)


def create_snapshot(root: Path, manifest_path: Path, output: Path) -> SnapshotReceipt:
    """Copy exactly one current manifest into an immutable QA-only tree."""
    manifest = SnapshotManifest.model_validate_json(manifest_path.read_bytes())
    entries = _validated_entries(manifest)
    current = build_manifest(root)
    if current != manifest.model_dump(mode="json"):
        reason = "source manifest changed before snapshot materialization"
        raise ValueError(reason)

    output.mkdir(mode=0o700)
    try:
        regular_files, symlinks = _materialize_entries(root, output, entries)
        _freeze_entries(output, entries)
    except BaseException:
        _remove_partial_snapshot(output)
        raise
    return SnapshotReceipt(
        entry_count=manifest.entry_count,
        regular_files=regular_files,
        source_tree_sha256=manifest.source_tree_sha256,
        symlinks=symlinks,
    )


def main() -> None:
    """Materialize a requested snapshot and write its secret-free receipt."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--root", type=Path, required=True)
    _ = parser.add_argument("--manifest", type=Path, required=True)
    _ = parser.add_argument("--output", type=Path, required=True)
    _ = parser.add_argument("--receipt", type=Path, required=True)
    arguments = _Arguments()
    _ = parser.parse_args(namespace=arguments)
    receipt = create_snapshot(arguments.root, arguments.manifest, arguments.output)
    _ = arguments.receipt.write_text(
        json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
