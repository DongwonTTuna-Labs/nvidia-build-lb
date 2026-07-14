"""Build a deterministic manifest for the candidate Git source surface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

_CHUNK_BYTES = 1024 * 1024
_GIT_UNAVAILABLE = "git unavailable"


class ManifestEntry(TypedDict):
    """One candidate source path and the attributes that affect its digest."""

    path: str
    type: str
    mode: str
    payload_sha256: str


class SourceManifest(TypedDict):
    """Serialized candidate source manifest."""

    schema_version: int
    algorithm: str
    source_tree_sha256: str
    entry_count: int
    entries: list[ManifestEntry]


@dataclass(frozen=True, slots=True)
class CandidatePayload:
    """One no-follow candidate payload read from the source root."""

    kind: str
    mode: str
    payload: bytes


class _Arguments(argparse.Namespace):
    root: Path = Path.cwd()
    output: Path = Path()


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


def _stable_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _relative_parts(relative_text: str) -> tuple[str, ...]:
    relative = Path(relative_text)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        reason = "candidate path is not a closed relative path"
        raise ValueError(reason)
    return relative.parts


def read_candidate_entry(root: Path, relative_text: str) -> CandidatePayload:
    """Read one candidate leaf without following source-root or ancestor symlinks."""
    parts = _relative_parts(relative_text)
    parent = _open_directory_no_follow(root)
    try:
        directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
        for component in parts[:-1]:
            child = os.open(component, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        name = parts[-1]
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        mode = f"{stat.S_IMODE(before.st_mode):04o}"
        if stat.S_ISREG(before.st_mode):
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            try:
                opened_before = os.fstat(descriptor)
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, _CHUNK_BYTES):
                    chunks.append(chunk)
                opened_after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(opened_before.st_mode) or not (
                _stable_metadata(before)
                == _stable_metadata(opened_before)
                == _stable_metadata(opened_after)
                == _stable_metadata(after)
            ):
                reason = "candidate regular file changed during manifest read"
                raise ValueError(reason)
            return CandidatePayload(kind="regular", mode=mode, payload=b"".join(chunks))
        if stat.S_ISLNK(before.st_mode):
            target = os.fsencode(os.readlink(name, dir_fd=parent))
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _stable_metadata(before) != _stable_metadata(after):
                reason = "candidate symlink changed during manifest read"
                raise ValueError(reason)
            return CandidatePayload(kind="symlink", mode=mode, payload=target)
        reason = f"unsupported candidate path type: {relative_text!r}"
        raise ValueError(reason)
    finally:
        os.close(parent)


def build_manifest(root: Path) -> SourceManifest:
    """Hash the Git candidate surface including path type and exact mode."""
    root_descriptor = _open_directory_no_follow(root)
    os.close(root_descriptor)
    git = shutil.which("git")
    if git is None:
        raise RuntimeError(_GIT_UNAVAILABLE)
    listed = subprocess.run(  # noqa: S603 - resolved executable and fixed arguments.
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    paths = sorted(path for path in listed.split(b"\0") if path)
    paths = [path for path in paths if path != b".omo" and not path.startswith(b".omo/")]

    source_digest = hashlib.sha256()
    entries: list[ManifestEntry] = []
    for relative in paths:
        relative_text = os.fsdecode(relative)
        payload = read_candidate_entry(root, relative_text)
        payload_digest = hashlib.sha256(payload.payload).hexdigest()

        for field in (
            payload.kind.encode(),
            payload.mode.encode(),
            relative,
            payload_digest.encode(),
        ):
            source_digest.update(len(field).to_bytes(8, "big"))
            source_digest.update(field)
        entries.append(
            {
                "path": relative_text,
                "type": payload.kind,
                "mode": payload.mode,
                "payload_sha256": payload_digest,
            }
        )

    return {
        "schema_version": 1,
        "algorithm": "git-files-type-mode-path-payload-sha256-v1",
        "source_tree_sha256": source_digest.hexdigest(),
        "entry_count": len(entries),
        "entries": entries,
    }


def main() -> None:
    """Write the manifest requested by the candidate gate."""
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--root", type=Path, default=Path.cwd())
    _ = parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(namespace=_Arguments())
    manifest = build_manifest(args.root)
    _ = args.output.write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
