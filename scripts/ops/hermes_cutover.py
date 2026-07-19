#!/usr/bin/env python3
# ruff: noqa: BLE001, C901, EM101, EM102, PLR0911, PLR0912, PLR0913, PLR0915, S310, S603, T201, TRY300, TRY301
"""Perform a fail-closed Hermes two-file cutover without exposing credentials."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Final
from uuid import UUID, uuid4

_DOWNSTREAM_RE: Final[re.Pattern[str]] = re.compile(r"nblb_ds_[0-9a-f]{64}\Z")
_ADMIN_RE: Final[re.Pattern[str]] = re.compile(r"nblb_admin_[0-9a-f]{64}\Z")
_BACKUP_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UPSTREAM_RE: Final[re.Pattern[bytes]] = re.compile(rb"nvapi-[A-Za-z0-9_-]{20,}")
_TARGET_PROVIDER: Final[str] = "nvidia"
_TARGET_MODEL: Final[str] = "z-ai/glm-5.2"
_TARGET_BASE_URL: Final[str] = "http://127.0.0.1:2456/v1"
_HTTP_OK: Final[int] = 200
_EXPECTED_UPSTREAMS: Final[int] = 2
_TERMINAL_PHASES: Final[frozenset[str]] = frozenset(
    {"applied", "rolled_back", "reapplied", "aborted"}
)
_MANIFEST_STATUSES: Final[frozenset[str]] = frozenset(
    {"prepared", "applied", "rolled_back", "reapplied", "aborted"}
)
_EXPECTED_SCOPES: Final[tuple[str, str]] = ("models:read", "chat:write")
_PRIVATE_DIRECTORY_MODE: Final[int] = 0o700
_PRIVATE_FILE_MODE: Final[int] = 0o600
_FILE_MODE_MASK: Final[int] = 0o777
_MAX_HEADER_LENGTH: Final[int] = 512
_ASCII_SPACE: Final[int] = 0x20
_ASCII_DELETE: Final[int] = 0x7F
_MIN_MOUNTINFO_FIELDS: Final[int] = 10
_DELAYED_UPDATE_SERVICE: Final[str] = "agent-apps-delayed-update.service"
_DELAYED_UPDATE_WRAPPER: Final[str] = "/usr/local/libexec/nvidia-build-lb-agent-apps-delayed-update"


class CutoverError(RuntimeError):
    """A safe, credential-free operator error."""


@dataclass(frozen=True)
class Settings:
    """Host paths and local endpoints used by the operator helper."""

    data_dir: Path
    state_root: Path
    agent_compose: Path
    admin_token_file: Path
    api_env_file: Path
    docker: str
    lb_base_url: str
    hermes_base_url: str
    container_name: str

    @classmethod
    def production(cls) -> Settings:
        """Return the fixed production boundary."""
        return cls(
            data_dir=Path("/opt/agent-apps/data/hermes"),
            state_root=Path("/opt/nvidia-build-lb/hermes-cutover-state"),
            agent_compose=Path("/opt/agent-apps/tools/agent-compose"),
            admin_token_file=Path("/opt/nvidia-build-lb/secrets/admin_token"),
            api_env_file=Path("/opt/agent-apps/secrets/app.env"),
            docker="docker",
            lb_base_url="http://127.0.0.1:2456",
            hermes_base_url="http://127.0.0.1:8642",
            container_name="agent-hermes",
        )


@dataclass(frozen=True)
class FileState:
    """A compare-and-swap tuple for one regular file."""

    sha256: str
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class PairState:
    """The exact Hermes environment and configuration tuple."""

    env: FileState
    config: FileState


@dataclass(frozen=True)
class BackupRecord:
    """A fully validated immutable backup generation."""

    path: Path
    manifest: dict[str, object]
    source: PairState
    target: PairState
    env_payload: bytes
    config_payload: bytes
    contains_upstream: bool


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_uuid(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise CutoverError(f"{label}_invalid")
    try:
        parsed = UUID(value)
    except ValueError:
        raise CutoverError(f"{label}_invalid") from None
    if str(parsed) != value:
        raise CutoverError(f"{label}_invalid")
    return value


def _absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise CutoverError("absolute_path_required")
    return Path(os.path.abspath(path))  # noqa: PTH100 - normalize without resolving links.


def _require_no_symlink_components(path: Path, *, leaf_may_be_missing: bool = False) -> None:
    absolute = _absolute(path)
    candidates = [absolute, *absolute.parents]
    for index, candidate in enumerate(candidates):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if index == 0 and leaf_may_be_missing:
                continue
            raise CutoverError("path_component_missing") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise CutoverError("path_symlink_forbidden")
        if not stat.S_ISDIR(metadata.st_mode):
            raise CutoverError("path_component_not_directory")


def _private_directory(path: Path) -> Path:
    absolute = _absolute(path)
    _require_no_symlink_components(absolute.parent)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        absolute.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        _fsync_directory(absolute.parent)
        metadata = absolute.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise CutoverError("private_directory_invalid")
    if (
        metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise CutoverError("private_directory_metadata_invalid")
    return absolute


def _path_is_within(path: Path, parent: Path) -> bool:
    return path == parent or path.is_relative_to(parent)


def _decode_mountinfo_path(value: str) -> Path:
    decoded = re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )
    return Path(os.path.abspath(decoded))  # noqa: PTH100 - normalize without link traversal.


def _mountinfo_entries() -> tuple[tuple[str, Path, Path], ...]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        raise CutoverError("mount_inventory_unavailable") from None
    entries: list[tuple[str, Path, Path]] = []
    for line in lines:
        fields = line.split()
        if len(fields) < _MIN_MOUNTINFO_FIELDS or "-" not in fields:
            raise CutoverError("mount_inventory_invalid")
        entries.append(
            (
                fields[2],
                _decode_mountinfo_path(fields[3]),
                _decode_mountinfo_path(fields[4]),
            )
        )
    return tuple(entries)


def _mount_identity(path: Path) -> tuple[str, Path]:
    absolute = _absolute(path)
    matches = [entry for entry in _mountinfo_entries() if _path_is_within(absolute, entry[2])]
    if not matches:
        raise CutoverError("mount_identity_unavailable")
    device, root, mount_point = max(matches, key=lambda entry: len(entry[2].parts))
    relative = absolute.relative_to(mount_point)
    physical = Path(
        os.path.abspath(  # noqa: PTH100 - normalize mount root without link traversal.
            root / relative
        )
    )
    return device, physical


def _reject_nested_mounts(path: Path) -> None:
    absolute = _absolute(path)
    if any(_path_is_within(mount_point, absolute) for _, _, mount_point in _mountinfo_entries()):
        raise CutoverError("backup_nested_mount_forbidden")


def _container_mount_sources(settings: Settings) -> tuple[Path, ...]:
    output = _run(
        [
            settings.docker,
            "inspect",
            "--format",
            "{{json .Mounts}}",
            settings.container_name,
        ]
    )
    try:
        mounts = json.loads(output)
    except json.JSONDecodeError:
        raise CutoverError("hermes_mount_inventory_invalid") from None
    if not isinstance(mounts, list):
        raise CutoverError("hermes_mount_inventory_invalid")
    sources: list[Path] = []
    for item in mounts:
        if not isinstance(item, dict):
            raise CutoverError("hermes_mount_inventory_invalid")
        source = item.get("Source")
        if not isinstance(source, str) or not source.startswith("/"):
            raise CutoverError("hermes_mount_source_invalid")
        sources.append(Path(source).resolve(strict=True))
    return tuple(sources)


def _require_host_only_roots(settings: Settings, *roots: Path) -> None:
    data_real = settings.data_dir.resolve(strict=True)
    root_reals = tuple(root.resolve(strict=True) for root in roots)
    if any(_path_is_within(root, data_real) for root in root_reals):
        raise CutoverError("state_root_must_be_outside_hermes_mount")
    data_device, data_physical = _mount_identity(data_real)
    for root in root_reals:
        root_device, root_physical = _mount_identity(root)
        if root_device == data_device and _path_is_within(root_physical, data_physical):
            raise CutoverError("host_only_root_backed_by_hermes_mount")
    if settings.data_dir != Settings.production().data_dir:
        return
    mount_sources = _container_mount_sources(settings)
    if any(
        _path_is_within(root, source) or _path_is_within(source, root)
        for root in root_reals
        for source in mount_sources
    ):
        raise CutoverError("host_only_root_mounted_into_hermes")


def _safe_header_value(value: str, label: str) -> str:
    contains_control = any(
        ord(character) <= _ASCII_SPACE or ord(character) == _ASCII_DELETE for character in value
    )
    if not value or len(value) > _MAX_HEADER_LENGTH or contains_control:
        raise CutoverError(f"{label}_header_invalid")
    return value


def _read_regular(path: Path) -> tuple[bytes, FileState]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CutoverError(f"regular_file_required:{path.name}")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise CutoverError(f"regular_file_changed:{path.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read()
    finally:
        os.close(descriptor)
    return payload, FileState(
        sha256=hashlib.sha256(payload).hexdigest(),
        uid=metadata.st_uid,
        gid=metadata.st_gid,
        mode=stat.S_IMODE(metadata.st_mode),
    )


def _pair_state(data_dir: Path) -> PairState:
    _, env = _read_regular(data_dir / ".env")
    _, config = _read_regular(data_dir / "config.yaml")
    return PairState(env=env, config=config)


def _require_secret_custody(state: FileState) -> None:
    if state.uid != os.geteuid() or state.gid != os.getegid() or state.mode != _PRIVATE_FILE_MODE:
        raise CutoverError("hermes_env_custody_invalid")


def _require_pair(actual: PairState, expected: PairState, label: str) -> None:
    if actual != expected:
        raise CutoverError(f"pair_compare_failed:{label}")


def _write_file(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, uid, gid)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        descriptor = -1
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_write(
    path: Path,
    payload: bytes,
    *,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
        raise CutoverError(f"atomic_target_invalid:{path.name}")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        _write_file(temporary, payload, mode=mode, uid=uid, gid=gid)
        temporary.replace(path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, document: dict[str, object]) -> None:
    payload = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _atomic_write(path, payload, mode=0o600, uid=os.geteuid(), gid=os.getegid())


def _cleanup_abandoned_atomic_temps(directory: Path, target_name: str) -> None:
    pattern = re.compile(rf"\.{re.escape(target_name)}\.[0-9a-f]{{32}}\.tmp\Z")
    removed = False
    with os.scandir(directory) as iterator:
        candidates = [entry for entry in iterator if pattern.fullmatch(entry.name)]
    for entry in candidates:
        metadata = entry.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise CutoverError("atomic_temporary_metadata_invalid")
        Path(entry.path).unlink()
        removed = True
    if removed:
        _fsync_directory(directory)


def _replace_env(payload: bytes, token: str) -> bytes:
    if not _DOWNSTREAM_RE.fullmatch(token):
        raise CutoverError("downstream_token_shape_invalid")
    text = payload.decode("utf-8")
    lines = text.splitlines(keepends=True)
    indexes = [index for index, line in enumerate(lines) if line.startswith("NVIDIA_API_KEY=")]
    if len(indexes) != 1:
        raise CutoverError("hermes_env_key_count_invalid")
    index = indexes[0]
    ending = "\n" if lines[index].endswith("\n") else ""
    lines[index] = f"NVIDIA_API_KEY={token}{ending}"
    replaced = "".join(lines).encode()
    if _UPSTREAM_RE.search(replaced):
        raise CutoverError("upstream_credential_remains_in_active_env")
    return replaced


def _replace_model_config(payload: bytes) -> bytes:
    text = payload.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "model:":
        raise CutoverError("hermes_model_block_missing")
    boundary = next(
        (index for index, line in enumerate(lines[1:], start=1) if line and not line[0].isspace()),
        len(lines),
    )
    replacements = {
        "default": _TARGET_MODEL,
        "provider": _TARGET_PROVIDER,
        "base_url": _TARGET_BASE_URL,
    }
    seen: set[str] = set()
    for index in range(1, boundary):
        match = re.fullmatch(r"  (default|provider|base_url):[^\r\n]*(\r?\n)?", lines[index])
        if match is None:
            continue
        key = match.group(1)
        if key in seen:
            raise CutoverError(f"hermes_model_field_duplicated:{key}")
        seen.add(key)
        lines[index] = f"  {key}: {replacements[key]}{match.group(2) or ''}"
    if seen != set(replacements):
        raise CutoverError("hermes_model_fields_incomplete")
    return "".join(lines).encode()


def _copy_backup(source: Path, destination: Path) -> None:
    metadata = source.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise CutoverError(f"backup_source_invalid:{source.name}")
    source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    destination_descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        opened_source = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(opened_source.st_mode)
            or opened_source.st_nlink != 1
            or (opened_source.st_dev, opened_source.st_ino) != (metadata.st_dev, metadata.st_ino)
        ):
            raise CutoverError(f"backup_source_changed:{source.name}")
        with (
            os.fdopen(source_descriptor, "rb", closefd=False) as source_handle,
            os.fdopen(destination_descriptor, "wb", closefd=False) as destination_handle,
        ):
            shutil.copyfileobj(source_handle, destination_handle)
            destination_handle.flush()
            os.fchmod(destination_descriptor, 0o600)
            os.fchown(destination_descriptor, os.geteuid(), os.getegid())
            os.fsync(destination_descriptor)
    finally:
        os.close(source_descriptor)
        os.close(destination_descriptor)


def _backup_root(settings: Settings) -> Path:
    state_root = _private_directory(settings.state_root)
    backup_root = _private_directory(state_root.parent / "hermes-cutover-backups")
    _require_host_only_roots(settings, state_root, backup_root)
    return backup_root


def _create_backup(
    settings: Settings,
    attempt_id: str,
    source: PairState,
    target: PairState,
    *,
    active_token_id: str | None,
    candidate_token_id: str,
) -> Path:
    if _BACKUP_ID_RE.fullmatch(attempt_id) is None:
        raise CutoverError("backup_id_invalid")
    if active_token_id is not None:
        _validated_uuid(active_token_id, "active_token_id")
    _validated_uuid(candidate_token_id, "candidate_token_id")
    backup_root = _backup_root(settings)
    backup = backup_root / attempt_id
    backup.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    os.chown(backup, os.geteuid(), os.getegid())
    backup.chmod(_PRIVATE_DIRECTORY_MODE)
    try:
        _copy_backup(settings.data_dir / ".env", backup / "env.before")
        _copy_backup(settings.data_dir / "config.yaml", backup / "config.before")
        manifest: dict[str, object] = {
            "schema_version": 1,
            "attempt_id": attempt_id,
            "created_at": _utc_stamp(),
            "source": asdict(source),
            "target": asdict(target),
            "active_token_id": active_token_id,
            "candidate_token_id": candidate_token_id,
            "status": "prepared",
        }
        _atomic_json(backup / "manifest.json", manifest)
        _fsync_directory(backup)
        _fsync_directory(backup_root)
        _ = _validate_backup(settings, backup)
    except Exception:
        for name in ("manifest.json", "config.before", "env.before"):
            (backup / name).unlink(missing_ok=True)
        backup.rmdir()
        _fsync_directory(backup_root)
        raise
    return backup


def _load_manifest(backup: Path) -> dict[str, object]:
    payload, _ = _read_regular(backup / "manifest.json")
    try:
        document: object = json.loads(payload)
    except json.JSONDecodeError:
        raise CutoverError("backup_manifest_invalid") from None
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise CutoverError("backup_manifest_invalid")
    return document


def _set_manifest_status(backup: Path, status_value: str) -> None:
    if status_value not in _MANIFEST_STATUSES:
        raise CutoverError("backup_status_invalid")
    manifest = _load_manifest(backup)
    manifest["status"] = status_value
    _atomic_json(backup / "manifest.json", manifest)
    _fsync_directory(backup)


def _file_state_from_document(value: object) -> FileState:
    if not isinstance(value, dict):
        raise CutoverError("backup_file_state_invalid")
    sha256 = value.get("sha256")
    uid = value.get("uid")
    gid = value.get("gid")
    mode = value.get("mode")
    if (
        not isinstance(sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid < 0
        or not isinstance(gid, int)
        or isinstance(gid, bool)
        or gid < 0
        or not isinstance(mode, int)
        or isinstance(mode, bool)
        or not 0 <= mode <= _FILE_MODE_MASK
    ):
        raise CutoverError("backup_file_state_invalid")
    return FileState(sha256=sha256, uid=uid, gid=gid, mode=mode)


def _state_from_document(value: object) -> PairState:
    if not isinstance(value, dict):
        raise CutoverError("backup_source_state_invalid")
    return PairState(
        env=_file_state_from_document(value.get("env")),
        config=_file_state_from_document(value.get("config")),
    )


def _validate_backup(settings: Settings, backup: Path) -> BackupRecord:
    backup_root = _backup_root(settings)
    backup = _absolute(backup)
    if backup.parent != backup_root or _BACKUP_ID_RE.fullmatch(backup.name) is None:
        raise CutoverError("backup_id_invalid")
    _reject_nested_mounts(backup)
    metadata = backup.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise CutoverError("backup_directory_metadata_invalid")
    _cleanup_abandoned_atomic_temps(backup, "manifest.json")
    with os.scandir(backup) as iterator:
        entry_names = {entry.name for entry in iterator}
    expected_names = {"env.before", "config.before", "manifest.json"}
    if entry_names != expected_names:
        raise CutoverError("backup_entries_invalid")
    payloads: dict[str, bytes] = {}
    for name in sorted(expected_names):
        path = backup / name
        entry_metadata = path.lstat()
        if (
            not stat.S_ISREG(entry_metadata.st_mode)
            or entry_metadata.st_nlink != 1
            or entry_metadata.st_uid != os.geteuid()
            or entry_metadata.st_gid != os.getegid()
            or stat.S_IMODE(entry_metadata.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise CutoverError("backup_file_metadata_invalid")
        payloads[name] = _read_regular(path)[0]
    manifest = _load_manifest(backup)
    if manifest.get("attempt_id") != backup.name:
        raise CutoverError("backup_manifest_attempt_mismatch")
    status_value = manifest.get("status")
    if not isinstance(status_value, str) or status_value not in _MANIFEST_STATUSES:
        raise CutoverError("backup_status_invalid")
    source = _state_from_document(manifest.get("source"))
    target = _state_from_document(manifest.get("target"))
    if hashlib.sha256(payloads["env.before"]).hexdigest() != source.env.sha256:
        raise CutoverError("backup_env_hash_mismatch")
    if hashlib.sha256(payloads["config.before"]).hexdigest() != source.config.sha256:
        raise CutoverError("backup_config_hash_mismatch")
    active_token_id = manifest.get("active_token_id")
    if active_token_id is not None:
        _validated_uuid(active_token_id, "backup_active_token_id")
    _validated_uuid(manifest.get("candidate_token_id"), "backup_candidate_token_id")
    return BackupRecord(
        path=backup,
        manifest=manifest,
        source=source,
        target=target,
        env_payload=payloads["env.before"],
        config_payload=payloads["config.before"],
        contains_upstream=any(_UPSTREAM_RE.search(payload) for payload in payloads.values()),
    )


def _discard_partial_backup(settings: Settings, backup: Path) -> None:
    backup_root = _backup_root(settings)
    backup = _absolute(backup)
    if backup.parent != backup_root or _BACKUP_ID_RE.fullmatch(backup.name) is None:
        raise CutoverError("partial_backup_id_invalid")
    try:
        metadata = backup.lstat()
    except FileNotFoundError:
        return
    _reject_nested_mounts(backup)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise CutoverError("partial_backup_directory_invalid")
    _cleanup_abandoned_atomic_temps(backup, "manifest.json")
    expected_names = {"env.before", "config.before", "manifest.json"}
    with os.scandir(backup) as iterator:
        entries = list(iterator)
    if any(entry.name not in expected_names for entry in entries):
        raise CutoverError("partial_backup_entries_invalid")
    for entry in entries:
        entry_metadata = entry.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(entry_metadata.st_mode)
            or entry_metadata.st_nlink != 1
            or entry_metadata.st_uid != os.geteuid()
            or entry_metadata.st_gid != os.getegid()
            or stat.S_IMODE(entry_metadata.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise CutoverError("partial_backup_file_invalid")
    for entry in entries:
        Path(entry.path).unlink()
    _fsync_directory(backup)
    backup.rmdir()
    _fsync_directory(backup_root)


def _retirement_tombstone(
    settings: Settings,
    backup_id: str,
) -> tuple[Path, dict[str, object] | None]:
    backup_root = _backup_root(settings)
    tombstone = backup_root / f".retiring-{backup_id}"
    metadata = tombstone.lstat()
    _reject_nested_mounts(tombstone)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
    ):
        raise CutoverError("retirement_directory_invalid")
    with os.scandir(tombstone) as iterator:
        entry_names = {entry.name for entry in iterator}
    expected_names = {"env.before", "config.before", "manifest.json"}
    if not entry_names.issubset(expected_names):
        raise CutoverError("retirement_entries_invalid")
    if "manifest.json" not in entry_names:
        if entry_names:
            raise CutoverError("retirement_manifest_missing")
        return tombstone, None
    _, manifest_state = _read_regular(tombstone / "manifest.json")
    if (
        manifest_state.uid != os.geteuid()
        or manifest_state.gid != os.getegid()
        or manifest_state.mode != _PRIVATE_FILE_MODE
    ):
        raise CutoverError("retirement_manifest_invalid")
    manifest = _load_manifest(tombstone)
    if manifest.get("attempt_id") != backup_id:
        raise CutoverError("retirement_attempt_mismatch")
    source = _state_from_document(manifest.get("source"))
    expected_states = {
        "env.before": source.env,
        "config.before": source.config,
    }
    for name, expected in expected_states.items():
        path = tombstone / name
        if not path.exists():
            continue
        payload, actual = _read_regular(path)
        if (
            actual.uid != os.geteuid()
            or actual.gid != os.getegid()
            or actual.mode != _PRIVATE_FILE_MODE
            or actual
            != FileState(
                sha256=expected.sha256,
                uid=os.geteuid(),
                gid=os.getegid(),
                mode=_PRIVATE_FILE_MODE,
            )
            or hashlib.sha256(payload).hexdigest() != expected.sha256
        ):
            raise CutoverError("retirement_file_invalid")
    requires_provider = manifest.get("retirement_requires_provider_confirmation")
    if not isinstance(requires_provider, bool):
        raise CutoverError("retirement_manifest_invalid")
    return tombstone, manifest


def _retirement_receipt_path(settings: Settings, backup_id: str) -> Path:
    if _BACKUP_ID_RE.fullmatch(backup_id) is None:
        raise CutoverError("backup_id_invalid")
    directory = _private_directory(settings.state_root / "retirements")
    return directory / f"{backup_id}.json"


def _load_retirement_receipt(
    settings: Settings,
    backup_id: str,
) -> dict[str, object] | None:
    path = _retirement_receipt_path(settings, backup_id)
    try:
        payload, file_state = _read_regular(path)
    except FileNotFoundError:
        return None
    if (
        file_state.uid != os.geteuid()
        or file_state.gid != os.getegid()
        or file_state.mode != _PRIVATE_FILE_MODE
    ):
        raise CutoverError("retirement_receipt_metadata_invalid")
    try:
        document: object = json.loads(payload)
    except json.JSONDecodeError:
        raise CutoverError("retirement_receipt_invalid") from None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("operation") != "retire-backup"
        or document.get("backup_id") != backup_id
        or document.get("status") not in {"pending", "complete"}
        or not isinstance(document.get("requires_provider_confirmation"), bool)
        or not isinstance(document.get("provider_revocation_confirmed"), bool)
    ):
        raise CutoverError("retirement_receipt_invalid")
    if (
        document["requires_provider_confirmation"] is True
        and document["provider_revocation_confirmed"] is not True
    ):
        raise CutoverError("retirement_receipt_invalid")
    return document


def _write_retirement_receipt(
    settings: Settings,
    backup_id: str,
    *,
    status_value: str,
    requires_provider_confirmation: bool,
    provider_revocation_confirmed: bool,
) -> dict[str, object]:
    if status_value not in {"pending", "complete"}:
        raise CutoverError("retirement_receipt_status_invalid")
    if requires_provider_confirmation and not provider_revocation_confirmed:
        raise CutoverError("provider_revocation_confirmation_required")
    document: dict[str, object] = {
        "schema_version": 1,
        "operation": "retire-backup",
        "backup_id": backup_id,
        "status": status_value,
        "requires_provider_confirmation": requires_provider_confirmation,
        "provider_revocation_confirmed": provider_revocation_confirmed,
    }
    _atomic_json(_retirement_receipt_path(settings, backup_id), document)
    return document


def _retirement_result(
    backup_id: str,
    receipt: dict[str, object],
) -> dict[str, object]:
    return {
        "status": "PASS",
        "operation": "retire-backup",
        "backup_id": backup_id,
        "provider_revocation_confirmed": receipt["provider_revocation_confirmed"],
        "backup_absent": True,
        "retirement_receipt": "complete",
    }


def _restore_backup(settings: Settings, backup: Path, expected: PairState) -> None:
    record = _validate_backup(settings, backup)
    if record.source != expected:
        raise CutoverError("backup_source_state_mismatch")
    _atomic_write(
        settings.data_dir / ".env",
        record.env_payload,
        mode=expected.env.mode,
        uid=expected.env.uid,
        gid=expected.env.gid,
    )
    _atomic_write(
        settings.data_dir / "config.yaml",
        record.config_payload,
        mode=expected.config.mode,
        uid=expected.config.uid,
        gid=expected.config.gid,
    )
    _require_pair(_pair_state(settings.data_dir), expected, "restored")


def _run(command: list[str], *, timeout: int = 120) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise CutoverError(f"command_failed:{Path(command[0]).name}:{completed.returncode}")
    return completed.stdout


def _compose(settings: Settings, *arguments: str) -> None:
    _run([str(settings.agent_compose), *arguments], timeout=180)


def _container_running(settings: Settings) -> bool:
    output = _run(
        [settings.docker, "inspect", "--format", "{{.State.Running}}", settings.container_name]
    )
    return output.strip() == "true"


def _wait_hermes_health(settings: Settings, timeout: int = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            health_url = f"{settings.hermes_base_url}/health"
            with urllib.request.urlopen(health_url, timeout=2) as response:
                if response.status == _HTTP_OK:
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(1)
    raise CutoverError("hermes_health_timeout")


def _other_container_ids(settings: Settings) -> dict[str, str]:
    output = _run(
        [
            settings.docker,
            "ps",
            "--filter",
            "label=com.docker.compose.project=agent-apps",
            "--format",
            "{{.Names}}|{{.ID}}",
        ]
    )
    identities: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, container_id = line.partition("|")
        if separator and name != settings.container_name:
            identities[name] = container_id
    codex = _run(
        [settings.docker, "ps", "--filter", "name=^codex-lb$", "--format", "{{.Names}}|{{.ID}}"]
    )
    for line in codex.splitlines():
        name, separator, container_id = line.partition("|")
        if separator:
            identities[name] = container_id
    return identities


def _credential_from_env(path: Path, name: str) -> str:
    payload, _ = _read_regular(path)
    prefix = f"{name}="
    matches = [
        line.removeprefix(prefix)
        for line in payload.decode("utf-8").splitlines()
        if line.startswith(prefix)
    ]
    if len(matches) != 1 or not matches[0]:
        raise CutoverError(f"credential_variable_invalid:{name}")
    return _safe_header_value(matches[0], name.lower())


def _request(
    method: str,
    url: str,
    bearer: str,
    *,
    payload: dict[str, object] | None = None,
    expected: tuple[int, ...] = (200,),
    timeout: int = 360,
    host: str | None = None,
) -> tuple[str, bytes]:
    bearer = _safe_header_value(bearer, "bearer")
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    headers = {"Authorization": f"Bearer {bearer}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if host is not None:
        headers["Host"] = host
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
            media_type = response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        body = error.read()
        status = error.code
        media_type = error.headers.get_content_type()
    if status not in expected:
        raise CutoverError(f"http_status_unexpected:{url.rsplit('/', 1)[-1]}:{status}")
    return media_type, body


def _json_body(body: bytes) -> dict[str, object]:
    document = json.loads(body)
    if not isinstance(document, dict):
        raise CutoverError("json_object_required")
    return document


def _admin(
    settings: Settings,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    expected: tuple[int, ...] = (_HTTP_OK,),
) -> dict[str, object]:
    admin_token = _read_regular(settings.admin_token_file)[0].decode("utf-8")
    if _ADMIN_RE.fullmatch(admin_token) is None:
        raise CutoverError("admin_token_file_invalid")
    _, body = _request(
        method,
        f"{settings.lb_base_url}/admin/api/v1{path}",
        admin_token,
        payload=payload,
        expected=expected,
        timeout=30,
        host="127.0.0.1:2456",
    )
    return {} if not body else _json_body(body)


def _admin_post(settings: Settings, path: str) -> None:
    # Rust gateway state aliases return the updated redacted key as 200;
    # older deployments used an empty 204 response. Accept both while the
    # helper remains compatible with either canonical runtime.
    _admin(settings, "POST", path, expected=(200, 204))


def _token_item(settings: Settings, token_id: str) -> dict[str, object]:
    items = _admin(settings, "GET", "/downstream-tokens").get("items")
    if not isinstance(items, list):
        raise CutoverError("downstream_list_invalid")
    for item in items:
        if isinstance(item, dict) and item.get("id") == token_id:
            return item
    raise CutoverError("downstream_token_not_found")


def _active_token_counts(settings: Settings) -> dict[str, int]:
    items = _admin(settings, "GET", "/downstream-tokens").get("items")
    if not isinstance(items, list):
        raise CutoverError("downstream_list_invalid")
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("revoked_at") is not None:
            continue
        token_id = item.get("id")
        count = item.get("request_count")
        if not isinstance(token_id, str) or not isinstance(count, int):
            raise CutoverError("downstream_item_invalid")
        counts[token_id] = count
    return counts


def _discover_hermes_token_id(settings: Settings) -> str:
    api_key = _credential_from_env(settings.api_env_file, "API_SERVER_KEY")
    before = _active_token_counts(settings)
    _hermes_chat(
        settings,
        api_key,
        "도구를 사용하지 말고 한국어로 정확히 '헤르메스 연결 성공'이라고만 답하세요.",
        stream=False,
    )
    after = _active_token_counts(settings)
    changed = [
        token_id for token_id, count in after.items() if count - before.get(token_id, count) == 1
    ]
    if len(changed) != 1:
        raise CutoverError("hermes_token_discovery_ambiguous")
    token_id = changed[0]
    _ = _validate_token_item(settings, token_id)
    return token_id


def _issue_downstream_token(settings: Settings, label: str) -> tuple[str, str]:
    response = _admin(
        settings,
        "POST",
        "/downstream-tokens",
        payload={"label": label, "scopes": list(_EXPECTED_SCOPES)},
        expected=(201,),
    )
    token_id = response.get("id")
    token = response.get("token")
    if not isinstance(token_id, str) or not isinstance(token, str):
        raise CutoverError("downstream_issue_response_invalid")
    _validated_uuid(token_id, "downstream_token_id")
    if not _DOWNSTREAM_RE.fullmatch(token):
        raise CutoverError("downstream_issue_token_invalid")
    return token_id, token


def _validate_token_item(
    settings: Settings,
    token_id: str,
    *,
    expected_label: str | None = None,
) -> dict[str, object]:
    _validated_uuid(token_id, "downstream_token_id")
    item = _token_item(settings, token_id)
    if item.get("revoked_at") is not None:
        raise CutoverError("downstream_token_revoked")
    scopes = item.get("scopes")
    if scopes != list(_EXPECTED_SCOPES):
        raise CutoverError("downstream_token_scopes_invalid")
    if expected_label is not None and item.get("label") != expected_label:
        raise CutoverError("downstream_token_label_mismatch")
    return item


def _bind_bearer_to_token_id(
    settings: Settings,
    token_id: str,
    bearer: str,
    *,
    expected_label: str | None = None,
    expected_before_count: int | None = None,
) -> None:
    if _DOWNSTREAM_RE.fullmatch(bearer) is None:
        raise CutoverError("downstream_token_shape_invalid")
    before_item = _validate_token_item(settings, token_id, expected_label=expected_label)
    before_count = before_item.get("request_count")
    if not isinstance(before_count, int):
        raise CutoverError("downstream_counter_invalid")
    if expected_before_count is not None and before_count != expected_before_count:
        raise CutoverError("downstream_binding_counter_drift")
    media_type, body = _request(
        "GET",
        f"{settings.lb_base_url}/v1/models",
        bearer,
        expected=(_HTTP_OK,),
        timeout=30,
        host="127.0.0.1:2456",
    )
    document = _json_body(body)
    if media_type != "application/json" or not isinstance(document.get("data"), list):
        raise CutoverError("downstream_binding_probe_invalid")
    after_item = _validate_token_item(settings, token_id, expected_label=expected_label)
    after_count = after_item.get("request_count")
    if not isinstance(after_count, int) or after_count != before_count + 1:
        raise CutoverError("downstream_bearer_token_id_mismatch")


def _revoke_and_verify(settings: Settings, token_id: str) -> None:
    item = _token_item(settings, token_id)
    if item.get("revoked_at") is None:
        try:
            _admin(settings, "DELETE", f"/downstream-tokens/{token_id}", expected=(204,))
        except (OSError, urllib.error.URLError, CutoverError):
            if _token_item(settings, token_id).get("revoked_at") is None:
                raise CutoverError("downstream_token_revoke_outcome_unknown") from None
    if _token_item(settings, token_id).get("revoked_at") is None:
        raise CutoverError("downstream_token_revoke_unconfirmed")


def _upstream_counts(settings: Settings) -> tuple[dict[str, int], list[str]]:
    items = _admin(settings, "GET", "/upstream-keys").get("items")
    if not isinstance(items, list):
        raise CutoverError("upstream_list_invalid")
    counts: dict[str, int] = {}
    eligible: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise CutoverError("upstream_item_invalid")
        key_id = item["id"]
        count = item.get("request_count")
        if not isinstance(count, int):
            raise CutoverError("upstream_counter_invalid")
        counts[key_id] = count
        cooldown_until = item.get("cooldown_until")
        cooldown_active = False
        if isinstance(cooldown_until, str):
            try:
                cooldown_active = datetime.fromisoformat(cooldown_until.replace("Z", "+00:00")) > datetime.now(UTC)
            except ValueError:
                raise CutoverError("upstream_cooldown_invalid") from None
        elif cooldown_until is not None:
            raise CutoverError("upstream_cooldown_invalid")
        if item.get("enabled") is True and item.get("verified") is True and not cooldown_active:
            eligible.append(key_id)
    return counts, eligible


def _token_count(settings: Settings, token_id: str) -> int:
    item = _validate_token_item(settings, token_id)
    count = item.get("request_count")
    if not isinstance(count, int):
        raise CutoverError("downstream_counter_invalid")
    return count


def _hermes_chat(settings: Settings, api_key: str, text: str, *, stream: bool) -> None:
    media_type, body = _request(
        "POST",
        f"{settings.hermes_base_url}/v1/chat/completions",
        api_key,
        payload={
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": text}],
            "stream": stream,
        },
    )
    if stream:
        terminator = b"data: [DONE]\n\n"
        if (
            media_type != "text/event-stream"
            or body.count(terminator) != 1
            or not body.endswith(terminator)
        ):
            raise CutoverError("hermes_stream_contract_failed")
        if "스트리밍 연결 성공".encode() not in body:
            raise CutoverError("hermes_stream_content_failed")
        return
    document = _json_body(body)
    choices = document.get("choices")
    if media_type != "application/json" or not isinstance(choices, list) or not choices:
        raise CutoverError("hermes_nonstream_contract_failed")
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or "헤르메스 연결 성공" not in content:
        raise CutoverError("hermes_nonstream_content_failed")


def _run_events(settings: Settings, api_key: str, run_id: str) -> tuple[str, ...]:
    request = urllib.request.Request(
        f"{settings.hermes_base_url}/v1/runs/{run_id}/events",
        headers={"Authorization": f"Bearer {api_key}"},
        method="GET",
    )
    event_names: list[str] = []
    with urllib.request.urlopen(request, timeout=420) as response:
        for raw_line in response:
            if not raw_line.startswith(b"data: "):
                continue
            document = _json_body(raw_line[6:].rstrip(b"\r\n"))
            event = document.get("event")
            if isinstance(event, str):
                event_names.append(event)
            if event in {"run.completed", "run.failed", "run.cancelled"}:
                break
    return tuple(event_names)


def _hermes_tool_run(settings: Settings, api_key: str) -> None:
    _, body = _request(
        "POST",
        f"{settings.hermes_base_url}/v1/runs",
        api_key,
        payload={
            "input": (
                "터미널 도구를 반드시 한 번 사용해 현재 디렉터리에서 pwd를 실행하세요. "
                "명령이 성공하면 한국어로 정확히 '도구 작업 성공'이라고만 답하세요."
            ),
            "model": "hermes-agent",
        },
        expected=(202,),
        timeout=30,
    )
    run_id = _json_body(body).get("run_id")
    if not isinstance(run_id, str):
        raise CutoverError("hermes_run_id_invalid")
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        events_future = executor.submit(_run_events, settings, api_key, run_id)
        terminal: dict[str, object] | None = None
        for _ in range(420):
            _, status_body = _request(
                "GET",
                f"{settings.hermes_base_url}/v1/runs/{run_id}",
                api_key,
                timeout=10,
            )
            terminal = _json_body(status_body)
            status = terminal.get("status")
            if status == "waiting_for_approval":
                _request(
                    "POST",
                    f"{settings.hermes_base_url}/v1/runs/{run_id}/stop",
                    api_key,
                    timeout=30,
                )
                raise CutoverError("hermes_tool_approval_unexpected")
            if status in {"completed", "failed", "cancelled"}:
                break
            time.sleep(1)
        if terminal is None or terminal.get("status") != "completed":
            raise CutoverError("hermes_tool_run_failed")
        output = terminal.get("output")
        if not isinstance(output, str) or "도구 작업 성공" not in output:
            raise CutoverError("hermes_tool_run_output_invalid")
        events = events_future.result(timeout=20)
    if "tool.started" not in events or "tool.completed" not in events:
        raise CutoverError("hermes_tool_evidence_missing")


def _verify_live(
    settings: Settings,
    token_id: str,
    *,
    include_tool: bool = True,
) -> dict[str, object]:
    token_id = _validated_uuid(token_id, "downstream_token_id")
    api_key = _credential_from_env(settings.api_env_file, "API_SERVER_KEY")
    before_upstream, eligible = _upstream_counts(settings)
    if len(eligible) != _EXPECTED_UPSTREAMS:
        raise CutoverError("two_eligible_upstreams_required")
    before_downstream = _token_count(settings, token_id)
    _wait_hermes_health(settings)
    _hermes_chat(
        settings,
        api_key,
        "도구를 사용하지 말고 한국어로 정확히 '헤르메스 연결 성공'이라고만 답하세요.",
        stream=False,
    )
    _hermes_chat(
        settings,
        api_key,
        "도구를 사용하지 말고 한국어로 정확히 '스트리밍 연결 성공'이라고만 답하세요.",
        stream=True,
    )
    if include_tool:
        _hermes_tool_run(settings, api_key)
    after_upstream, _ = _upstream_counts(settings)
    downstream_delta = _token_count(settings, token_id) - before_downstream
    upstream_deltas = {
        key_id: after_upstream[key_id] - before_upstream[key_id] for key_id in eligible
    }
    minimum = 3 if include_tool else 2
    if downstream_delta < minimum or any(delta < 1 for delta in upstream_deltas.values()):
        raise CutoverError("hermes_routing_evidence_failed")
    return {
        "health": True,
        "nonstream": True,
        "stream": True,
        "tool_run": include_tool,
        "downstream_request_delta": downstream_delta,
        "upstream_request_deltas": upstream_deltas,
    }


def _journal_path(settings: Settings) -> Path:
    return settings.state_root / "journal.json"


def _update_journal(settings: Settings, document: dict[str, object], phase: str) -> None:
    document["phase"] = phase
    document["updated_at"] = _utc_stamp()
    serialized = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if _UPSTREAM_RE.search(serialized) or b"nblb_ds_" in serialized:
        raise CutoverError("journal_secret_material_forbidden")
    _atomic_json(_journal_path(settings), document)


def _journal_document(settings: Settings) -> dict[str, object] | None:
    path = _journal_path(settings)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE
    ):
        raise CutoverError("journal_file_metadata_invalid")
    return _json_body(_read_regular(path)[0])


def _ensure_hermes_started(settings: Settings) -> None:
    if not _container_running(settings):
        _compose(settings, "up", "-d", "--force-recreate", "--no-deps", "hermes")
    _wait_hermes_health(settings)


def _pair_is_known(current: PairState, source: PairState, target: PairState) -> bool:
    return current in {source, target} or (
        current.env in {source.env, target.env} and current.config in {source.config, target.config}
    )


def _verify_generation(
    settings: Settings,
    expected: PairState,
    token_id: str | None,
    *,
    include_tool: bool,
) -> dict[str, object]:
    _require_pair(_pair_state(settings.data_dir), expected, "running_generation")
    _ensure_hermes_started(settings)
    if token_id is not None:
        validated_token_id = _validated_uuid(token_id, "generation_token_id")
        return _verify_live(settings, validated_token_id, include_tool=include_tool)
    env_payload, _ = _read_regular(settings.data_dir / ".env")
    if _UPSTREAM_RE.search(env_payload) is None:
        raise CutoverError("generation_token_id_required")
    api_key = _credential_from_env(settings.api_env_file, "API_SERVER_KEY")
    _hermes_chat(
        settings,
        api_key,
        "도구를 사용하지 말고 한국어로 정확히 '헤르메스 연결 성공'이라고만 답하세요.",
        stream=False,
    )
    _hermes_chat(
        settings,
        api_key,
        "도구를 사용하지 말고 한국어로 정확히 '스트리밍 연결 성공'이라고만 답하세요.",
        stream=True,
    )
    if include_tool:
        _hermes_tool_run(settings, api_key)
    return {
        "health": True,
        "nonstream": True,
        "stream": True,
        "tool_run": include_tool,
        "generation": "upstream",
    }


def _close_intake_locked(
    settings: Settings,
    journal: dict[str, object],
    phase: str,
) -> None:
    if _container_running(settings):
        _compose(settings, "stop", "hermes")
    if _container_running(settings):
        raise CutoverError("hermes_intake_close_unconfirmed")
    _update_journal(settings, journal, phase)


def _journal_backup(settings: Settings, journal: dict[str, object], field: str) -> BackupRecord:
    backup_id = journal.get(field)
    if not isinstance(backup_id, str) or _BACKUP_ID_RE.fullmatch(backup_id) is None:
        raise CutoverError("journal_backup_id_invalid")
    return _validate_backup(settings, _backup_root(settings) / backup_id)


def _journal_states(journal: dict[str, object]) -> tuple[PairState, PairState]:
    return (
        _state_from_document(journal.get("source")),
        _state_from_document(journal.get("target")),
    )


def _mark_recovery_required(
    settings: Settings,
    journal: dict[str, object],
    failed_phase: object,
) -> None:
    if isinstance(failed_phase, str) and failed_phase != "recovery_required":
        journal["failed_phase"] = failed_phase
    _update_journal(settings, journal, "recovery_required")


def _candidate_revocable(journal: dict[str, object]) -> bool:
    provenance = journal.get("candidate_provenance")
    if provenance is not None:
        if provenance not in {"helper_issued", "binding_confirmed"}:
            raise CutoverError("candidate_provenance_invalid")
        return True
    if journal.get("candidate_owned") is not True:
        return False
    operation = journal.get("operation")
    if operation in {"cycle", "rollback"}:
        return True
    phase = journal.get("phase")
    return isinstance(phase, str) and phase not in {
        "starting",
        "backup_creating",
        "prepared",
        "cutover_intake_closed",
        "candidate_binding_pending",
    }


def _manual_candidate_reconciliation_needed(journal: dict[str, object]) -> bool:
    if journal.get("operation") != "cutover" or journal.get("candidate_token_id") is None:
        return False
    review = journal.get("manual_candidate_requires_review")
    confirmed = journal.get("candidate_revoked_confirmed")
    if review is not None and not isinstance(review, bool):
        raise CutoverError("manual_candidate_review_state_invalid")
    if confirmed is not None and not isinstance(confirmed, bool):
        raise CutoverError("candidate_revocation_state_invalid")
    if confirmed is True:
        return False
    if _candidate_revocable(journal):
        return False
    if review is False:
        raise CutoverError("manual_candidate_review_state_invalid")
    return True


def _entry_recovery_required(journal: dict[str, object]) -> bool:
    phase = journal.get("phase")
    if not isinstance(phase, str) or phase not in _TERMINAL_PHASES:
        return True
    if phase not in {"aborted", "rolled_back"} or journal.get("candidate_token_id") is None:
        return False
    confirmed = journal.get("candidate_revoked_confirmed")
    if confirmed is not None and not isinstance(confirmed, bool):
        raise CutoverError("candidate_revocation_state_invalid")
    return confirmed is not True


def _candidate_reconciliation_receipt(
    journal: dict[str, object],
    *,
    changed: bool,
) -> dict[str, object]:
    terminal = journal.get("reconciliation_terminal_phase")
    if terminal not in {"aborted", "rolled_back"}:
        raise CutoverError("candidate_reconciliation_terminal_invalid")
    candidate = _validated_uuid(
        journal.get("candidate_token_id"),
        "journal_candidate_token_id",
    )
    return {
        "status": "ACTION_REQUIRED",
        "operation": "recover",
        "phase": "candidate_reconciliation_required",
        "changed": changed,
        "next_action": "review_and_revoke_candidate_token",
        "candidate_token_id": candidate,
        "candidate_revoked_confirmed": False,
        "manual_candidate_requires_review": True,
        "reconciliation_terminal_phase": terminal,
    }


def _operation_reconciliation_receipt(
    recovery: dict[str, object],
    operation: str,
) -> dict[str, object]:
    return {
        **recovery,
        "operation": operation,
        "recovery_operation": "recover",
        "error": f"{operation}_blocked_candidate_reconciliation_required",
    }


def _resolve_failed_candidate_locked(
    settings: Settings,
    journal: dict[str, object],
    terminal_phase: str,
) -> dict[str, object] | None:
    if journal.get("candidate_token_id") is None:
        return None
    candidate = _validated_uuid(
        journal.get("candidate_token_id"),
        "journal_candidate_token_id",
    )
    if _candidate_revocable(journal):
        _revoke_and_verify(settings, candidate)
        journal["candidate_revoked_confirmed"] = True
        journal["manual_candidate_requires_review"] = False
        return None
    if not _manual_candidate_reconciliation_needed(journal):
        return None
    if _token_item(settings, candidate).get("revoked_at") is not None:
        journal["candidate_revoked_confirmed"] = True
        journal["manual_candidate_requires_review"] = False
        return None
    journal["candidate_revoked_confirmed"] = False
    journal["manual_candidate_requires_review"] = True
    journal["reconciliation_terminal_phase"] = terminal_phase
    _update_journal(settings, journal, "candidate_reconciliation_required")
    return _candidate_reconciliation_receipt(journal, changed=True)


def _recover_candidate_reconciliation_locked(
    settings: Settings,
    journal: dict[str, object],
) -> dict[str, object]:
    terminal = journal.get("reconciliation_terminal_phase")
    if not isinstance(terminal, str) or terminal not in {"aborted", "rolled_back"}:
        raise CutoverError("candidate_reconciliation_terminal_invalid")
    candidate = _validated_uuid(
        journal.get("candidate_token_id"),
        "journal_candidate_token_id",
    )
    if _token_item(settings, candidate).get("revoked_at") is None:
        return _candidate_reconciliation_receipt(journal, changed=False)
    journal["candidate_revoked_confirmed"] = True
    journal["manual_candidate_requires_review"] = False
    _update_journal(settings, journal, terminal)
    return _verify_terminal_journal_locked(settings, journal, terminal)


def _recovery_receipt(
    journal: dict[str, object],
    phase: str,
    *,
    changed: bool,
) -> dict[str, object]:
    next_action = {
        "aborted": "issue_new_candidate",
        "rolled_back": "issue_new_candidate",
        "applied": "verify_or_rollback",
        "reapplied": "retire_backups_after_revocations",
    }.get(phase)
    if next_action is None:
        raise CutoverError("recovery_phase_invalid")
    receipt: dict[str, object] = {
        "status": "PASS",
        "operation": "recover",
        "phase": phase,
        "changed": changed,
        "next_action": next_action,
    }
    for field in ("backup_id", "reapply_backup_id"):
        value = journal.get(field)
        if value is None:
            continue
        if not isinstance(value, str) or _BACKUP_ID_RE.fullmatch(value) is None:
            raise CutoverError("journal_backup_id_invalid")
        receipt[field] = value
    candidate = journal.get("candidate_token_id")
    if candidate is not None:
        receipt["candidate_token_id"] = _validated_uuid(
            candidate,
            "journal_candidate_token_id",
        )
    if phase in {"aborted", "rolled_back"} and candidate is not None:
        confirmed = journal.get("candidate_revoked_confirmed")
        review = journal.get("manual_candidate_requires_review")
        if not isinstance(confirmed, bool) or not isinstance(review, bool):
            raise CutoverError("candidate_reconciliation_state_invalid")
        receipt["candidate_revoked_confirmed"] = confirmed
        receipt["manual_candidate_requires_review"] = review
    return receipt


def _abort_unreferenced_locked(
    settings: Settings,
    journal: dict[str, object],
) -> dict[str, object]:
    _cleanup_recorded_candidates(settings, journal)
    source_value = journal.get("source")
    if source_value is not None:
        source = _state_from_document(source_value)
        _require_pair(_pair_state(settings.data_dir), source, "unreferenced_source")
    _ensure_hermes_started(settings)
    reconciliation = _resolve_failed_candidate_locked(settings, journal, "aborted")
    if reconciliation is not None:
        return reconciliation
    _update_journal(settings, journal, "aborted")
    return _recovery_receipt(journal, "aborted", changed=True)


def _recover_original_generation_locked(
    settings: Settings,
    journal: dict[str, object],
) -> dict[str, object]:
    source, target = _journal_states(journal)
    backup = _journal_backup(settings, journal, "backup_id")
    if backup.source != source or backup.target != target:
        raise CutoverError("journal_backup_state_mismatch")
    _cleanup_recorded_candidates(settings, journal)
    current = _pair_state(settings.data_dir)
    if not _pair_is_known(current, source, target):
        failed_phase = journal.get("failed_phase", journal.get("phase"))
        _close_intake_locked(settings, journal, "recovery_intake_closed")
        _mark_recovery_required(settings, journal, failed_phase)
        raise CutoverError("unknown_pair_recovery_required")
    if current != source:
        _close_intake_locked(settings, journal, "recovery_intake_closed")
        _replace_from_backup_locked(settings, backup, current, journal, "recovery_source")
    previous_token_id = journal.get("previous_token_id")
    if previous_token_id is not None and not isinstance(previous_token_id, str):
        raise CutoverError("journal_previous_token_id_invalid")
    try:
        _ = _verify_generation(
            settings,
            source,
            previous_token_id,
            include_tool=False,
        )
    except Exception:
        _close_intake_locked(settings, journal, "recovery_verification_failed")
        _mark_recovery_required(settings, journal, "recovery_verification_failed")
        raise
    candidate_token_id = _validated_uuid(
        journal.get("candidate_token_id"),
        "journal_candidate_token_id",
    )
    if previous_token_id == candidate_token_id:
        raise CutoverError("journal_token_ids_not_distinct")
    _set_manifest_status(backup.path, "rolled_back")
    reapply_id = journal.get("reapply_backup_id")
    if isinstance(reapply_id, str):
        reapply_path = _backup_root(settings) / reapply_id
        if reapply_path.exists():
            reapply = _validate_backup(settings, reapply_path)
            _set_manifest_status(reapply.path, "aborted")
    reconciliation = _resolve_failed_candidate_locked(settings, journal, "rolled_back")
    if reconciliation is not None:
        return reconciliation
    _update_journal(settings, journal, "rolled_back")
    return _recovery_receipt(journal, "rolled_back", changed=True)


def _recover_committed_cycle_locked(
    settings: Settings,
    journal: dict[str, object],
) -> dict[str, object]:
    source, target = _journal_states(journal)
    original = _journal_backup(settings, journal, "backup_id")
    reapply = _journal_backup(settings, journal, "reapply_backup_id")
    if original.source != source or original.target != target or reapply.source != target:
        raise CutoverError("journal_backup_state_mismatch")
    _cleanup_recorded_candidates(settings, journal)
    current = _pair_state(settings.data_dir)
    if not _pair_is_known(current, source, target):
        failed_phase = journal.get("failed_phase", journal.get("phase"))
        _close_intake_locked(settings, journal, "recovery_intake_closed")
        _mark_recovery_required(settings, journal, failed_phase)
        raise CutoverError("unknown_pair_recovery_required")
    if current != target:
        _close_intake_locked(settings, journal, "recovery_intake_closed")
        _replace_from_backup_locked(settings, reapply, current, journal, "recovery_target")
    candidate_token_id = _validated_uuid(
        journal.get("candidate_token_id"),
        "journal_candidate_token_id",
    )
    try:
        _ = _verify_generation(settings, target, candidate_token_id, include_tool=False)
    except Exception:
        _close_intake_locked(settings, journal, "recovery_verification_failed")
        _mark_recovery_required(settings, journal, "recovery_verification_failed")
        raise
    previous_value = journal.get("previous_token_id")
    previous_token_id = (
        _validated_uuid(previous_value, "journal_previous_token_id")
        if previous_value is not None
        else None
    )
    if previous_token_id == candidate_token_id:
        raise CutoverError("journal_token_ids_not_distinct")
    if previous_token_id is not None:
        _revoke_and_verify(settings, previous_token_id)
    _set_manifest_status(original.path, "reapplied")
    _set_manifest_status(reapply.path, "reapplied")
    _update_journal(settings, journal, "reapplied")
    return _recovery_receipt(journal, "reapplied", changed=True)


def _verify_terminal_journal_locked(
    settings: Settings,
    journal: dict[str, object],
    phase: str,
) -> dict[str, object]:
    if phase == "aborted":
        _cleanup_recorded_candidates(settings, journal)
        if _candidate_revocable(journal):
            candidate_token_id = _validated_uuid(
                journal.get("candidate_token_id"),
                "journal_candidate_token_id",
            )
            _revoke_and_verify(settings, candidate_token_id)
            journal["candidate_revoked_confirmed"] = True
            journal["manual_candidate_requires_review"] = False
            _update_journal(settings, journal, phase)
        return _recovery_receipt(journal, phase, changed=False)
    source, target = _journal_states(journal)
    _cleanup_recorded_candidates(settings, journal)
    expected = target if phase in {"applied", "reapplied"} else source
    token_value = (
        journal.get("candidate_token_id")
        if phase in {"applied", "reapplied"}
        else journal.get("previous_token_id")
    )
    token_id = token_value if isinstance(token_value, str) else None
    try:
        _ = _verify_generation(settings, expected, token_id, include_tool=False)
    except Exception:
        _close_intake_locked(settings, journal, "terminal_verification_failed")
        _mark_recovery_required(settings, journal, "terminal_verification_failed")
        raise
    if phase == "rolled_back" and _candidate_revocable(journal):
        candidate_token_id = _validated_uuid(
            journal.get("candidate_token_id"),
            "journal_candidate_token_id",
        )
        _revoke_and_verify(settings, candidate_token_id)
        journal["candidate_revoked_confirmed"] = True
        journal["manual_candidate_requires_review"] = False
        _update_journal(settings, journal, phase)
    if phase == "reapplied":
        previous_value = journal.get("previous_token_id")
        if previous_value is not None:
            previous_token_id = _validated_uuid(
                previous_value,
                "journal_previous_token_id",
            )
            _revoke_and_verify(settings, previous_token_id)
    return _recovery_receipt(journal, phase, changed=False)


def _recover_issuance_locked(
    settings: Settings,
    journal: dict[str, object],
) -> dict[str, object]:
    _cleanup_recorded_candidates(settings, journal)
    label = journal.get("issuance_label")
    if not isinstance(label, str):
        raise CutoverError("issuance_journal_label_invalid")
    matches = _tokens_with_label(settings, label)
    if len(matches) > 1:
        raise CutoverError("issuance_reconciliation_ambiguous")
    recorded = journal.get("candidate_token_id")
    if recorded is None:
        if not matches:
            _update_journal(settings, journal, "aborted")
            return _recovery_receipt(journal, "aborted", changed=True)
        candidate = _validated_uuid(
            matches[0].get("id"),
            "issuance_reconciliation_id",
        )
        journal["candidate_token_id"] = candidate
        journal["candidate_provenance"] = "helper_issued"
        journal["candidate_owned"] = True
        _update_journal(settings, journal, "issued_unreferenced")
    else:
        candidate = _validated_uuid(recorded, "journal_candidate_token_id")
        if not matches:
            raise CutoverError("issuance_reconciliation_recorded_candidate_missing")
        matched = _validated_uuid(
            matches[0].get("id"),
            "issuance_reconciliation_id",
        )
        if matched != candidate:
            raise CutoverError("issuance_reconciliation_id_mismatch")
        if journal.get("candidate_provenance") != "helper_issued":
            raise CutoverError("issuance_reconciliation_provenance_invalid")
        if journal.get("candidate_owned") is not True:
            raise CutoverError("issuance_reconciliation_ownership_invalid")
        _update_journal(settings, journal, "issued_unreferenced")
    _revoke_and_verify(settings, candidate)
    journal["candidate_revoked_confirmed"] = True
    journal["manual_candidate_requires_review"] = False
    _update_journal(settings, journal, "aborted")
    return _recovery_receipt(journal, "aborted", changed=True)


def _recover_locked(
    settings: Settings,
    journal: dict[str, object],
) -> dict[str, object]:
    _restore_one_key_exclusion(settings, journal)
    phase = journal.get("phase")
    if not isinstance(phase, str):
        raise CutoverError("journal_phase_invalid")
    if phase == "candidate_reconciliation_required":
        return _recover_candidate_reconciliation_locked(settings, journal)
    if phase in {"aborted", "rolled_back"} and _manual_candidate_reconciliation_needed(journal):
        reconciliation = _resolve_failed_candidate_locked(settings, journal, phase)
        if reconciliation is not None:
            return reconciliation
        _update_journal(settings, journal, phase)
    if phase in _TERMINAL_PHASES:
        return _verify_terminal_journal_locked(settings, journal, phase)
    if phase in {"issuing", "issued_unreferenced"}:
        return _recover_issuance_locked(settings, journal)
    if phase == "recovery_required" and journal.get("failed_phase") in {
        "issuing",
        "issued_unreferenced",
    }:
        return _recover_issuance_locked(settings, journal)
    if phase == "starting":
        return _abort_unreferenced_locked(settings, journal)
    if phase in {"backup_creating", "cycle_reapply_backup_creating"}:
        field = "backup_id" if phase == "backup_creating" else "reapply_backup_id"
        backup_id = journal.get(field)
        if not isinstance(backup_id, str) or _BACKUP_ID_RE.fullmatch(backup_id) is None:
            raise CutoverError("journal_backup_id_invalid")
        backup_path = _backup_root(settings) / backup_id
        if not backup_path.exists():
            if phase == "backup_creating":
                return _abort_unreferenced_locked(settings, journal)
            return _recover_original_generation_locked(settings, journal)
        try:
            _ = _validate_backup(settings, backup_path)
        except CutoverError:
            _discard_partial_backup(settings, backup_path)
            if phase == "backup_creating":
                return _abort_unreferenced_locked(settings, journal)
            return _recover_original_generation_locked(settings, journal)
    operation = journal.get("operation")
    if operation == "cycle":
        if journal.get("commit_decided") is True:
            return _recover_committed_cycle_locked(settings, journal)
        return _recover_original_generation_locked(settings, journal)
    if operation == "rollback":
        return _recover_original_generation_locked(settings, journal)
    if operation != "cutover":
        raise CutoverError("journal_operation_invalid")
    source, target = _journal_states(journal)
    backup = _journal_backup(settings, journal, "backup_id")
    if backup.source != source or backup.target != target:
        raise CutoverError("journal_backup_state_mismatch")
    current = _pair_state(settings.data_dir)
    if current == target:
        candidate_token_id = _validated_uuid(
            journal.get("candidate_token_id"),
            "journal_candidate_token_id",
        )
        try:
            _ = _verify_generation(settings, target, candidate_token_id, include_tool=True)
        except Exception:
            return _recover_original_generation_locked(settings, journal)
        _set_manifest_status(backup.path, "applied")
        _update_journal(settings, journal, "applied")
        return _recovery_receipt(journal, "applied", changed=True)
    return _recover_original_generation_locked(settings, journal)


def _recover(settings: Settings) -> dict[str, object]:
    with _acquire_lock(settings):
        journal = _journal_document(settings)
        if journal is None:
            return {
                "status": "PASS",
                "operation": "recover",
                "phase": "absent",
                "changed": False,
                "next_action": "issue_new_candidate",
            }
        return _recover_locked(settings, journal)


def _prepare_state_root(settings: Settings) -> None:
    state_root = _private_directory(settings.state_root)
    backup_root = _private_directory(state_root.parent / "hermes-cutover-backups")
    _require_host_only_roots(settings, state_root, backup_root)


def _acquire_lock(settings: Settings) -> BinaryIO:
    _prepare_state_root(settings)
    path = settings.state_root / "cutover.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE
    ):
        os.close(descriptor)
        raise CutoverError("cutover_lock_invalid")
    handle = os.fdopen(descriptor, "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def _require_delayed_update_guard(settings: Settings) -> bool:
    if settings.data_dir != Settings.production().data_dir:
        return True
    output = _run(
        [
            "systemctl",
            "show",
            _DELAYED_UPDATE_SERVICE,
            "--property=ActiveState",
            "--property=ExecStart",
        ],
        timeout=30,
    )
    properties = dict(line.split("=", maxsplit=1) for line in output.splitlines() if "=" in line)
    if properties.get("ActiveState") != "inactive":
        raise CutoverError("delayed_update_service_not_inactive")
    effective = properties.get("ExecStart", "")
    if _DELAYED_UPDATE_WRAPPER not in effective:
        raise CutoverError("delayed_update_lock_wrapper_missing")
    return True


def _preflight_locked(settings: Settings) -> dict[str, object]:
    current_journal = _journal_document(settings)
    if current_journal is not None and _entry_recovery_required(current_journal):
        recovery = _recover_locked(settings, current_journal)
        if recovery.get("status") == "ACTION_REQUIRED":
            return _operation_reconciliation_receipt(recovery, "preflight")
    env_payload, env_state = _read_regular(settings.data_dir / ".env")
    _require_secret_custody(env_state)
    config_payload, _ = _read_regular(settings.data_dir / "config.yaml")
    credential_class = "upstream" if _UPSTREAM_RE.search(env_payload) else "downstream"
    if credential_class == "downstream" and b"NVIDIA_API_KEY=nblb_ds_" not in env_payload:
        credential_class = "other"
    config_text = config_payload.decode("utf-8")
    return {
        "status": "PASS",
        "container_running": _container_running(settings),
        "credential_class": credential_class,
        "provider_target": f"  provider: {_TARGET_PROVIDER}" in config_text,
        "model_target": f"  default: {_TARGET_MODEL}" in config_text,
        "base_url_target": f"  base_url: {_TARGET_BASE_URL}" in config_text,
        "backup_outside_mount": True,
        "delayed_update_lock_guard": _require_delayed_update_guard(settings),
        "issuance_allowed": True,
    }


def _preflight(settings: Settings) -> dict[str, object]:
    with _acquire_lock(settings):
        return _preflight_locked(settings)


def _target_payloads(
    settings: Settings,
    source: PairState,
    token: str,
) -> tuple[bytes, bytes, PairState]:
    _require_secret_custody(source.env)
    env_payload, _ = _read_regular(settings.data_dir / ".env")
    config_payload, _ = _read_regular(settings.data_dir / "config.yaml")
    target_env = _replace_env(env_payload, token)
    target_config = _replace_model_config(config_payload)
    target = PairState(
        env=FileState(
            sha256=hashlib.sha256(target_env).hexdigest(),
            uid=source.env.uid,
            gid=source.env.gid,
            mode=source.env.mode,
        ),
        config=FileState(
            sha256=hashlib.sha256(target_config).hexdigest(),
            uid=source.config.uid,
            gid=source.config.gid,
            mode=source.config.mode,
        ),
    )
    return target_env, target_config, target


def _register_candidate_files(
    settings: Settings,
    journal: dict[str, object],
    env_candidate: Path,
    config_candidate: Path,
    target: PairState,
    phase: str,
) -> None:
    if env_candidate.parent != settings.data_dir or config_candidate.parent != settings.data_dir:
        raise CutoverError("candidate_parent_invalid")
    if any(settings.data_dir.glob(".nblb-*.candidate")):
        raise CutoverError("unrecorded_candidate_file_present")
    journal["candidate_files"] = {
        "env": {"name": env_candidate.name, "state": asdict(target.env)},
        "config": {"name": config_candidate.name, "state": asdict(target.config)},
    }
    journal["candidate_files_cleaned"] = False
    _update_journal(settings, journal, phase)


def _cleanup_recorded_candidates(
    settings: Settings,
    journal: dict[str, object],
) -> None:
    candidate_files = journal.get("candidate_files")
    phase = journal.get("phase")
    incomplete_staging = isinstance(phase, str) and phase.endswith("_candidate_staging")
    observed = {path.name for path in settings.data_dir.glob(".nblb-*.candidate")}
    if candidate_files is None:
        if observed:
            raise CutoverError("unrecorded_candidate_file_present")
        return
    if not isinstance(candidate_files, dict) or set(candidate_files) != {"env", "config"}:
        raise CutoverError("candidate_journal_invalid")
    recorded_names = {
        item.get("name") for item in candidate_files.values() if isinstance(item, dict)
    }
    if observed - recorded_names:
        raise CutoverError("unrecorded_candidate_file_present")
    for key in ("env", "config"):
        item = candidate_files.get(key)
        if not isinstance(item, dict):
            raise CutoverError("candidate_journal_invalid")
        name = item.get("name")
        expected_pattern = rf"[.]nblb-[A-Za-z0-9._-]+[.]{key}[.]candidate"
        if not isinstance(name, str) or re.fullmatch(expected_pattern, name) is None:
            raise CutoverError("candidate_name_invalid")
        expected = _file_state_from_document(item.get("state"))
        path = settings.data_dir / name
        try:
            _, actual = _read_regular(path)
        except FileNotFoundError:
            continue
        if actual != expected and (not incomplete_staging or actual.uid != os.geteuid()):
            raise CutoverError("candidate_state_invalid")
        path.unlink()
    _fsync_directory(settings.data_dir)
    if any(settings.data_dir.glob(".nblb-*.candidate")):
        raise CutoverError("candidate_cleanup_incomplete")
    journal["candidate_files_cleaned"] = True


def _stage_targets(
    settings: Settings,
    attempt_id: str,
    source: PairState,
    env_payload: bytes,
    config_payload: bytes,
) -> tuple[Path, Path]:
    env_candidate = settings.data_dir / f".nblb-{attempt_id}.env.candidate"
    config_candidate = settings.data_dir / f".nblb-{attempt_id}.config.candidate"
    env_created = False
    config_created = False
    try:
        _write_file(
            env_candidate,
            env_payload,
            mode=source.env.mode,
            uid=source.env.uid,
            gid=source.env.gid,
        )
        env_created = True
        _write_file(
            config_candidate,
            config_payload,
            mode=source.config.mode,
            uid=source.config.uid,
            gid=source.config.gid,
        )
        config_created = True
        _fsync_directory(settings.data_dir)
        return env_candidate, config_candidate
    except Exception:
        if env_created:
            env_candidate.unlink(missing_ok=True)
        if config_created:
            config_candidate.unlink(missing_ok=True)
        _fsync_directory(settings.data_dir)
        raise


def _replace_pair(
    settings: Settings,
    env_candidate: Path,
    config_candidate: Path,
    target: PairState,
    journal: dict[str, object],
    *,
    phase_prefix: str = "cutover",
) -> None:
    production = settings.data_dir == Settings.production().data_dir
    if production and _container_running(settings):
        raise CutoverError("hermes_restarted_before_pair_replace")
    _update_journal(settings, journal, f"{phase_prefix}_env_replace_started")
    _ = env_candidate.replace(settings.data_dir / ".env")
    _fsync_directory(settings.data_dir)
    _update_journal(settings, journal, f"{phase_prefix}_env_replaced")
    if os.environ.get("NBLB_HERMES_INJECT_FAILURE") == "after_env_replace":
        raise CutoverError("injected_after_env_replace")
    if production and _container_running(settings):
        raise CutoverError("hermes_restarted_during_pair_replace")
    _update_journal(settings, journal, f"{phase_prefix}_config_replace_started")
    _ = config_candidate.replace(settings.data_dir / "config.yaml")
    _fsync_directory(settings.data_dir)
    _update_journal(settings, journal, f"{phase_prefix}_pair_replaced")
    _require_pair(_pair_state(settings.data_dir), target, "target")


def _replace_from_backup_locked(
    settings: Settings,
    record: BackupRecord,
    current: PairState,
    journal: dict[str, object],
    phase_prefix: str,
) -> None:
    _require_pair(_pair_state(settings.data_dir), current, f"{phase_prefix}_source")
    attempt_id = str(journal.get("attempt_id", "transaction"))
    candidate_attempt = f"{attempt_id}-{phase_prefix}"
    env_path = settings.data_dir / f".nblb-{candidate_attempt}.env.candidate"
    config_path = settings.data_dir / f".nblb-{candidate_attempt}.config.candidate"
    _register_candidate_files(
        settings,
        journal,
        env_path,
        config_path,
        record.source,
        f"{phase_prefix}_candidate_staging",
    )
    env_candidate, config_candidate = _stage_targets(
        settings,
        candidate_attempt,
        record.source,
        record.env_payload,
        record.config_payload,
    )
    try:
        _update_journal(settings, journal, f"{phase_prefix}_candidates_staged")
        _replace_pair(
            settings,
            env_candidate,
            config_candidate,
            record.source,
            journal,
            phase_prefix=phase_prefix,
        )
        _cleanup_recorded_candidates(settings, journal)
        _update_journal(settings, journal, f"{phase_prefix}_candidates_cleaned")
    finally:
        _cleanup_recorded_candidates(settings, journal)


def _perform_cutover_locked(
    settings: Settings,
    token: str,
    token_id: str,
    previous_token_id: str | None,
    journal: dict[str, object],
    *,
    attempt_id: str,
    terminal_phase: str,
    expected_label: str | None,
) -> dict[str, object]:
    token_id = _validated_uuid(token_id, "candidate_token_id")
    if previous_token_id is not None:
        previous_token_id = _validated_uuid(previous_token_id, "previous_token_id")
        if previous_token_id == token_id:
            raise CutoverError("candidate_and_previous_token_must_differ")
    if _DOWNSTREAM_RE.fullmatch(token) is None:
        raise CutoverError("downstream_token_shape_invalid")
    other_before = _other_container_ids(settings)
    source = _pair_state(settings.data_dir)
    target_env, target_config, target = _target_payloads(settings, source, token)
    journal.update(
        {
            "backup_id": attempt_id,
            "source": asdict(source),
            "target": asdict(target),
            "candidate_token_id": token_id,
            "previous_token_id": previous_token_id,
        }
    )
    _update_journal(settings, journal, "backup_creating")
    backup = _create_backup(
        settings,
        attempt_id,
        source,
        target,
        active_token_id=previous_token_id,
        candidate_token_id=token_id,
    )
    if backup.name != attempt_id:
        raise CutoverError("backup_attempt_id_mismatch")
    _update_journal(settings, journal, "prepared")
    env_candidate = settings.data_dir / f".nblb-{attempt_id}.env.candidate"
    config_candidate = settings.data_dir / f".nblb-{attempt_id}.config.candidate"
    try:
        _close_intake_locked(settings, journal, "cutover_intake_closed")
        _require_pair(_pair_state(settings.data_dir), source, "cutover_prewrite")
        candidate_item = _validate_token_item(
            settings,
            token_id,
            expected_label=expected_label,
        )
        binding_before_count = candidate_item.get("request_count")
        if not isinstance(binding_before_count, int):
            raise CutoverError("downstream_counter_invalid")
        journal["binding_before_count"] = binding_before_count
        _update_journal(settings, journal, "candidate_binding_pending")
        _bind_bearer_to_token_id(
            settings,
            token_id,
            token,
            expected_label=expected_label,
            expected_before_count=binding_before_count,
        )
        journal["candidate_provenance"] = "binding_confirmed"
        journal["candidate_owned"] = True
        journal["manual_candidate_requires_review"] = False
        _update_journal(settings, journal, "candidate_bound")
        _register_candidate_files(
            settings,
            journal,
            env_candidate,
            config_candidate,
            target,
            "cutover_candidate_staging",
        )
        env_candidate, config_candidate = _stage_targets(
            settings,
            attempt_id,
            source,
            target_env,
            target_config,
        )
        _update_journal(settings, journal, "cutover_candidates_staged")
        _replace_pair(
            settings,
            env_candidate,
            config_candidate,
            target,
            journal,
            phase_prefix="cutover",
        )
        _compose(settings, "up", "-d", "--force-recreate", "--no-deps", "hermes")
        _wait_hermes_health(settings)
        _update_journal(settings, journal, "cutover_gateway_started")
        live = _verify_live(settings, token_id)
        if _other_container_ids(settings) != other_before:
            raise CutoverError("unrelated_container_identity_changed")
        _set_manifest_status(backup, "applied")
        _cleanup_recorded_candidates(settings, journal)
        _update_journal(settings, journal, terminal_phase)
        return {
            "status": "PASS",
            "operation": "cutover",
            "backup_id": backup.name,
            "candidate_token_id": token_id,
            "live": live,
        }
    finally:
        _cleanup_recorded_candidates(settings, journal)


def _rollback(settings: Settings, backup_id: str, candidate_token_id: str) -> dict[str, object]:
    candidate_token_id = _validated_uuid(candidate_token_id, "candidate_token_id")
    with _acquire_lock(settings):
        current_journal = _journal_document(settings)
        if current_journal is not None and _entry_recovery_required(current_journal):
            recovery = _recover_locked(settings, current_journal)
            if recovery.get("status") == "ACTION_REQUIRED":
                return _operation_reconciliation_receipt(recovery, "rollback")
        record = _validate_backup(settings, _backup_root(settings) / backup_id)
        if record.manifest.get("candidate_token_id") != candidate_token_id:
            raise CutoverError("rollback_candidate_token_mismatch")
        if record.manifest.get("status") != "applied":
            raise CutoverError("rollback_backup_status_invalid")
        _require_pair(_pair_state(settings.data_dir), record.target, "rollback_source")
        previous_token_id = record.manifest.get("active_token_id")
        if previous_token_id is not None and not isinstance(previous_token_id, str):
            raise CutoverError("backup_active_token_id_invalid")
        journal: dict[str, object] = {
            "schema_version": 1,
            "operation": "rollback",
            "attempt_id": f"rollback-{uuid4().hex}",
            "backup_id": backup_id,
            "candidate_token_id": candidate_token_id,
            "previous_token_id": previous_token_id,
            "candidate_owned": True,
            "candidate_provenance": "binding_confirmed",
            "source": asdict(record.source),
            "target": asdict(record.target),
        }
        _update_journal(settings, journal, "rollback_prepared")
        try:
            _close_intake_locked(settings, journal, "rollback_intake_closed")
            _require_pair(_pair_state(settings.data_dir), record.target, "rollback_prewrite")
            _replace_from_backup_locked(
                settings,
                record,
                record.target,
                journal,
                "rollback",
            )
            _ = _verify_generation(
                settings,
                record.source,
                previous_token_id,
                include_tool=False,
            )
            _update_journal(settings, journal, "rollback_verified")
            _revoke_and_verify(settings, candidate_token_id)
            _set_manifest_status(record.path, "rolled_back")
            _update_journal(settings, journal, "rolled_back")
        except Exception:
            failed_phase = journal.get("phase")
            journal["failed_phase"] = failed_phase
            try:
                recovery_journal = _journal_document(settings)
                if recovery_journal is None:
                    _ensure_hermes_started(settings)
                else:
                    _ = _recover_locked(settings, recovery_journal)
            except Exception:
                recovery_journal = _journal_document(settings) or journal
                _mark_recovery_required(settings, recovery_journal, failed_phase)
                raise CutoverError("rollback_failed_recovery_required") from None
            raise
    return {
        "status": "PASS",
        "operation": "rollback",
        "backup_id": backup_id,
        "candidate_token_id": candidate_token_id,
        "revoked_confirmed": True,
    }


def _swap_generation_locked(
    settings: Settings,
    *,
    target_backup: BackupRecord,
    current_state: PairState,
    target_token_id: str | None,
    journal: dict[str, object],
    phase_prefix: str,
) -> dict[str, object]:
    _require_pair(_pair_state(settings.data_dir), current_state, f"{phase_prefix}_source")
    _close_intake_locked(settings, journal, f"{phase_prefix}_intake_closed")
    _require_pair(_pair_state(settings.data_dir), current_state, f"{phase_prefix}_prewrite")
    _replace_from_backup_locked(
        settings,
        target_backup,
        current_state,
        journal,
        phase_prefix,
    )
    live = _verify_generation(
        settings,
        target_backup.source,
        target_token_id,
        include_tool=False,
    )
    _update_journal(settings, journal, f"{phase_prefix}_verified")
    return live


def _restore_one_key_exclusion(
    settings: Settings,
    journal: dict[str, object],
) -> None:
    value = journal.get("one_key_exclusion")
    if value is None:
        return
    if not isinstance(value, dict):
        raise CutoverError("one_key_journal_invalid")
    excluded = _validated_uuid(value.get("excluded_key_id"), "excluded_key_id")
    alternate = _validated_uuid(value.get("alternate_key_id"), "alternate_key_id")
    eligible_value = value.get("original_eligible_ids")
    if (
        excluded == alternate
        or not isinstance(eligible_value, list)
        or len(eligible_value) != _EXPECTED_UPSTREAMS
        or not all(isinstance(item, str) for item in eligible_value)
    ):
        raise CutoverError("one_key_journal_invalid")
    eligible = [_validated_uuid(item, "eligible_key_id") for item in eligible_value]
    if set(eligible) != {excluded, alternate}:
        raise CutoverError("one_key_journal_invalid")
    status_value = value.get("status")
    if status_value not in {"disable_pending", "disabled", "restored"}:
        raise CutoverError("one_key_journal_invalid")
    if status_value == "restored":
        return
    item = next(
        (
            candidate
            for candidate in _upstreams_for_recovery(settings)
            if candidate.get("id") == excluded
        ),
        None,
    )
    if item is None:
        raise CutoverError("excluded_key_missing_during_restore")
    if item.get("enabled") is not True:
        probe = _admin(settings, "POST", f"/upstream-keys/{excluded}/probe")
        if probe.get("probe_status") != "valid":
            raise CutoverError("excluded_key_reprobe_failed")
        _admin_post(settings, f"/upstream-keys/{excluded}/enable")
    _, restored = _upstream_counts(settings)
    if set(restored) != set(eligible):
        raise CutoverError("two_key_state_restore_failed")
    if status_value != "restored":
        value["status"] = "restored"
        _update_journal(settings, journal, "cycle_one_key_restored")


def _verify_one_key_exclusion(
    settings: Settings,
    journal: dict[str, object],
) -> dict[str, object]:
    before, eligible = _upstream_counts(settings)
    if len(eligible) != _EXPECTED_UPSTREAMS:
        raise CutoverError("two_eligible_upstreams_required")
    excluded, alternate = eligible
    api_key = _credential_from_env(settings.api_env_file, "API_SERVER_KEY")
    exclusion_state: dict[str, object] = {
        "excluded_key_id": excluded,
        "alternate_key_id": alternate,
        "original_eligible_ids": list(eligible),
        "status": "disable_pending",
    }
    journal["one_key_exclusion"] = exclusion_state
    _update_journal(settings, journal, "cycle_one_key_disable_pending")
    operation_error: BaseException | None = None
    try:
        _admin_post(settings, f"/upstream-keys/{excluded}/disable")
        exclusion_state["status"] = "disabled"
        _update_journal(settings, journal, "cycle_one_key_disabled")
        _, remaining = _upstream_counts(settings)
        if remaining != [alternate]:
            raise CutoverError("one_key_exclusion_unconfirmed")
        _hermes_chat(
            settings,
            api_key,
            "도구를 사용하지 말고 한국어로 정확히 '헤르메스 연결 성공'이라고만 답하세요.",
            stream=False,
        )
        after, _ = _upstream_counts(settings)
        if after[excluded] != before[excluded] or after[alternate] - before[alternate] < 1:
            raise CutoverError("one_key_exclusion_routing_failed")
    except BaseException as error:
        operation_error = error
    cleanup_error: Exception | None = None
    try:
        _restore_one_key_exclusion(settings, journal)
    except Exception as error:
        cleanup_error = error
    if cleanup_error is not None:
        raise CutoverError("excluded_key_restore_failed") from cleanup_error
    if operation_error is not None:
        raise operation_error
    return {
        "excluded_key_id": excluded,
        "alternate_key_id": alternate,
        "alternate_succeeded": True,
        "excluded_key_restored": True,
    }


def _upstreams_for_recovery(settings: Settings) -> list[dict[str, object]]:
    items = _admin(settings, "GET", "/upstream-keys").get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise CutoverError("upstream_list_invalid")
    return items


def _restart_and_verify(settings: Settings, token_id: str) -> dict[str, object]:
    identities = _other_container_ids(settings)
    _compose(settings, "up", "-d", "--force-recreate", "--no-deps", "hermes")
    _wait_hermes_health(settings)
    receipt = _verify_live(settings, token_id, include_tool=False)
    if _other_container_ids(settings) != identities:
        raise CutoverError("unrelated_container_identity_changed")
    return receipt


def _tokens_with_label(settings: Settings, label: str) -> list[dict[str, object]]:
    items = _admin(settings, "GET", "/downstream-tokens").get("items")
    if not isinstance(items, list):
        raise CutoverError("downstream_list_invalid")
    return [item for item in items if isinstance(item, dict) and item.get("label") == label]


def _issue_for_cycle(settings: Settings, label: str) -> tuple[str, str]:
    try:
        return _issue_downstream_token(settings, label)
    except (OSError, urllib.error.URLError, CutoverError):
        matches = _tokens_with_label(settings, label)
        if len(matches) == 1 and isinstance(matches[0].get("id"), str):
            token_id = str(matches[0]["id"])
            _revoke_and_verify(settings, token_id)
            raise CutoverError("ambiguous_issue_reconciled_and_revoked") from None
        if matches:
            raise CutoverError("ambiguous_issue_reconciliation_failed") from None
        raise


def _cycle(settings: Settings) -> dict[str, object]:
    with _acquire_lock(settings):
        current_journal = _journal_document(settings)
        if current_journal is not None and _entry_recovery_required(current_journal):
            recovery = _recover_locked(settings, current_journal)
            if recovery.get("status") == "ACTION_REQUIRED":
                return _operation_reconciliation_receipt(recovery, "cycle")
        other_before = _other_container_ids(settings)
        env_payload, _ = _read_regular(settings.data_dir / ".env")
        if _UPSTREAM_RE.search(env_payload):
            current_token_id = None
            previous_generation = "upstream"
        elif b"NVIDIA_API_KEY=nblb_ds_" in env_payload:
            current_token_id = _discover_hermes_token_id(settings)
            previous_generation = "downstream"
        else:
            raise CutoverError("cycle_requires_known_credential_generation")
        cycle_id = uuid4().hex
        label = f"hermes-cutover:{cycle_id}"
        journal: dict[str, object] = {
            "schema_version": 1,
            "operation": "cycle",
            "attempt_id": cycle_id,
            "issuance_label": label,
            "candidate_token_id": None,
            "previous_token_id": current_token_id,
            "commit_decided": False,
            "candidate_owned": False,
        }
        _update_journal(settings, journal, "issuing")
        try:
            candidate_token_id, candidate_token = _issue_for_cycle(settings, label)
            journal["candidate_token_id"] = candidate_token_id
            journal["candidate_provenance"] = "helper_issued"
            journal["candidate_owned"] = True
            _update_journal(settings, journal, "issued_unreferenced")
            cutover = _perform_cutover_locked(
                settings,
                candidate_token,
                candidate_token_id,
                current_token_id,
                journal,
                attempt_id=f"{_utc_stamp()}-{cycle_id[:12]}",
                terminal_phase="cycle_cutover_applied",
                expected_label=label,
            )
            current_backup = _journal_backup(settings, journal, "backup_id")
            candidate_state = _pair_state(settings.data_dir)
            if candidate_state != current_backup.target:
                raise CutoverError("cycle_candidate_state_mismatch")
            candidate_backup_id = f"{_utc_stamp()}-{uuid4().hex[:12]}-reapply"
            journal["reapply_backup_id"] = candidate_backup_id
            _update_journal(settings, journal, "cycle_reapply_backup_creating")
            candidate_backup = _create_backup(
                settings,
                candidate_backup_id,
                candidate_state,
                current_backup.source,
                active_token_id=candidate_token_id,
                candidate_token_id=candidate_token_id,
            )
            if candidate_backup.name != candidate_backup_id:
                raise CutoverError("reapply_backup_attempt_id_mismatch")
            _update_journal(settings, journal, "cycle_reapply_backup_created")
            candidate_record = _validate_backup(settings, candidate_backup)
            rollback_live = _swap_generation_locked(
                settings,
                target_backup=current_backup,
                current_state=candidate_state,
                target_token_id=current_token_id,
                journal=journal,
                phase_prefix="cycle_rollback",
            )
            reapply_live = _swap_generation_locked(
                settings,
                target_backup=candidate_record,
                current_state=current_backup.source,
                target_token_id=candidate_token_id,
                journal=journal,
                phase_prefix="cycle_reapply",
            )
            exclusion = _verify_one_key_exclusion(settings, journal)
            restart = _restart_and_verify(settings, candidate_token_id)
            if _other_container_ids(settings) != other_before:
                raise CutoverError("unrelated_container_identity_changed")
            decision = dict(journal)
            decision["commit_decided"] = True
            _update_journal(settings, decision, "cycle_previous_revoke_pending")
            persisted = _journal_document(settings)
            if (
                persisted is None
                or persisted.get("attempt_id") != cycle_id
                or persisted.get("commit_decided") is not True
                or persisted.get("phase") != "cycle_previous_revoke_pending"
            ):
                raise CutoverError("cycle_commit_decision_unconfirmed")
            journal = persisted
            if current_token_id is not None:
                _revoke_and_verify(settings, current_token_id)
            _set_manifest_status(current_backup.path, "reapplied")
            _set_manifest_status(candidate_record.path, "reapplied")
            _update_journal(settings, journal, "reapplied")
            return {
                "status": "PASS",
                "operation": "cycle",
                "previous_generation": previous_generation,
                "previous_token_id": current_token_id,
                "candidate_token_id": candidate_token_id,
                "rollback_backup_id": current_backup.path.name,
                "reapply_backup_id": candidate_record.path.name,
                "cutover": cutover["live"],
                "rollback": rollback_live,
                "reapply": reapply_live,
                "one_key_exclusion": exclusion,
                "restart": restart,
                "previous_token_revoked": current_token_id is None
                or _token_item(settings, current_token_id).get("revoked_at") is not None,
                "final_token_active": True,
            }
        except BaseException:
            failed_phase = journal.get("phase")
            journal["failed_phase"] = failed_phase
            try:
                recovery_journal = _journal_document(settings)
                if recovery_journal is None:
                    _ensure_hermes_started(settings)
                else:
                    _ = _recover_locked(settings, recovery_journal)
            except Exception:
                recovery_journal = _journal_document(settings) or journal
                _mark_recovery_required(settings, recovery_journal, failed_phase)
                raise CutoverError("cycle_failed_recovery_required") from None
            raise


def _retire_backup(
    settings: Settings,
    backup_id: str,
    *,
    provider_credential_revoked: bool,
) -> dict[str, object]:
    with _acquire_lock(settings):
        backup_root = _backup_root(settings)
        backup_path = backup_root / backup_id
        tombstone_path = backup_root / f".retiring-{backup_id}"
        receipt = _load_retirement_receipt(settings, backup_id)
        if receipt is not None and receipt["status"] == "complete":
            if backup_path.exists() or tombstone_path.exists():
                raise CutoverError("backup_retirement_state_ambiguous")
            return _retirement_result(backup_id, receipt)
        if backup_path.exists() and tombstone_path.exists():
            raise CutoverError("backup_retirement_state_ambiguous")
        if backup_path.exists():
            record = _validate_backup(settings, backup_path)
            if record.manifest.get("status") not in {
                "applied",
                "rolled_back",
                "reapplied",
                "aborted",
            }:
                raise CutoverError("backup_not_terminal")
            active_value = record.manifest.get("active_token_id")
            if active_value is not None:
                if not isinstance(active_value, str):
                    raise CutoverError("backup_active_token_id_invalid")
                if _token_item(settings, active_value).get("revoked_at") is None:
                    raise CutoverError("backup_still_contains_active_downstream_token")
            if (
                record.contains_upstream
                and not provider_credential_revoked
                and (receipt is None or receipt["provider_revocation_confirmed"] is not True)
            ):
                raise CutoverError("provider_revocation_confirmation_required")
            provider_confirmed = (
                provider_credential_revoked
                or not record.contains_upstream
                or (receipt is not None and receipt["provider_revocation_confirmed"] is True)
            )
            if receipt is None:
                receipt = _write_retirement_receipt(
                    settings,
                    backup_id,
                    status_value="pending",
                    requires_provider_confirmation=record.contains_upstream,
                    provider_revocation_confirmed=provider_confirmed,
                )
            elif (
                receipt["requires_provider_confirmation"] is not record.contains_upstream
                or receipt["provider_revocation_confirmed"] is not provider_confirmed
            ):
                raise CutoverError("retirement_receipt_mismatch")
            retirement_manifest = dict(record.manifest)
            retirement_manifest["retirement_requires_provider_confirmation"] = (
                record.contains_upstream
            )
            _atomic_json(record.path / "manifest.json", retirement_manifest)
            _ = _validate_backup(settings, record.path)
            record.path.rename(tombstone_path)
            _fsync_directory(backup_root)
        elif not tombstone_path.exists():
            if receipt is None:
                raise CutoverError("backup_generation_missing")
            _fsync_directory(backup_root)
            if backup_path.exists() or tombstone_path.exists():
                raise CutoverError("backup_retirement_state_ambiguous")
            receipt = _write_retirement_receipt(
                settings,
                backup_id,
                status_value="complete",
                requires_provider_confirmation=bool(receipt["requires_provider_confirmation"]),
                provider_revocation_confirmed=bool(receipt["provider_revocation_confirmed"]),
            )
            return _retirement_result(backup_id, receipt)
        _fsync_directory(backup_root)
        if backup_path.exists() or not tombstone_path.exists():
            raise CutoverError("backup_retirement_state_ambiguous")
        tombstone, manifest = _retirement_tombstone(settings, backup_id)
        if manifest is None:
            if receipt is None:
                raise CutoverError("retirement_receipt_missing")
            tombstone.rmdir()
            _fsync_directory(backup_root)
            receipt = _write_retirement_receipt(
                settings,
                backup_id,
                status_value="complete",
                requires_provider_confirmation=bool(receipt["requires_provider_confirmation"]),
                provider_revocation_confirmed=bool(receipt["provider_revocation_confirmed"]),
            )
            return _retirement_result(backup_id, receipt)
        active_token_id = manifest.get("active_token_id")
        if active_token_id is not None:
            if not isinstance(active_token_id, str):
                raise CutoverError("backup_active_token_id_invalid")
            if _token_item(settings, active_token_id).get("revoked_at") is None:
                raise CutoverError("backup_still_contains_active_downstream_token")
        requires_provider = manifest.get("retirement_requires_provider_confirmation") is True
        if (
            requires_provider
            and not provider_credential_revoked
            and (receipt is None or receipt["provider_revocation_confirmed"] is not True)
        ):
            raise CutoverError("provider_revocation_confirmation_required")
        if receipt is None:
            receipt = _write_retirement_receipt(
                settings,
                backup_id,
                status_value="pending",
                requires_provider_confirmation=requires_provider,
                provider_revocation_confirmed=provider_credential_revoked or not requires_provider,
            )
        elif receipt["requires_provider_confirmation"] is not requires_provider:
            raise CutoverError("retirement_receipt_mismatch")
        for name in ("env.before", "config.before", "manifest.json"):
            (tombstone / name).unlink(missing_ok=True)
            _fsync_directory(tombstone)
        tombstone.rmdir()
        _fsync_directory(backup_root)
        receipt = _write_retirement_receipt(
            settings,
            backup_id,
            status_value="complete",
            requires_provider_confirmation=requires_provider,
            provider_revocation_confirmed=bool(receipt["provider_revocation_confirmed"]),
        )
    return _retirement_result(backup_id, receipt)


def _rehearse(root: Path, *, inject_after_env: bool = False) -> dict[str, object]:
    if root.exists() and any(root.iterdir()):
        raise CutoverError("fixture_root_must_be_empty")
    data = root / "mounted-hermes"
    state = root / "host-only-state"
    data.mkdir(parents=True, mode=0o700)
    state.mkdir(mode=0o700)
    env = data / ".env"
    config = data / "config.yaml"
    env.write_bytes(b"OTHER=value\nNVIDIA_API_KEY=legacy-placeholder\n")
    config.write_bytes(
        b"model:\n  default: old\n  provider: nvidia\n  base_url: https://example.invalid/v1\n"
    )
    env.chmod(0o600)
    config.chmod(0o640)
    source = _pair_state(data)
    token = "nblb_ds_" + ("a" * 64)
    settings = Settings(
        data_dir=data,
        state_root=state,
        agent_compose=Path("/fixture/agent-compose"),
        admin_token_file=Path("/fixture/admin"),
        api_env_file=Path("/fixture/api"),
        docker="docker",
        lb_base_url="http://127.0.0.1:2456",
        hermes_base_url="http://127.0.0.1:8642",
        container_name="agent-hermes",
    )
    attempt_id = "fixture-attempt"
    target_env, target_config, target = _target_payloads(settings, source, token)
    backup = _create_backup(
        settings,
        attempt_id,
        source,
        target,
        active_token_id=None,
        candidate_token_id=str(UUID(int=1)),
    )
    env_candidate, config_candidate = _stage_targets(
        settings,
        attempt_id,
        source,
        target_env,
        target_config,
    )
    journal: dict[str, object] = {"schema_version": 1, "attempt_id": attempt_id}
    prior_injection = os.environ.get("NBLB_HERMES_INJECT_FAILURE")
    if inject_after_env:
        os.environ["NBLB_HERMES_INJECT_FAILURE"] = "after_env_replace"
    try:
        _replace_pair(settings, env_candidate, config_candidate, target, journal)
    except CutoverError:
        if not inject_after_env:
            raise
        _restore_backup(settings, backup, source)
        return {
            "status": "PASS",
            "operation": "rehearse",
            "atomic_replace": True,
            "pair_rollback": True,
            "injected_failure_recovered": True,
            "backup_outside_mount": not state.is_relative_to(data),
        }
    finally:
        env_candidate.unlink(missing_ok=True)
        config_candidate.unlink(missing_ok=True)
        _fsync_directory(data)
        if prior_injection is None:
            os.environ.pop("NBLB_HERMES_INJECT_FAILURE", None)
        else:
            os.environ["NBLB_HERMES_INJECT_FAILURE"] = prior_injection
    _require_pair(_pair_state(data), target, "fixture_target")
    _restore_backup(settings, backup, source)
    return {
        "status": "PASS",
        "operation": "rehearse",
        "atomic_replace": True,
        "pair_rollback": True,
        "backup_outside_mount": not state.is_relative_to(data),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    subparsers.add_parser("recover")
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--backup-id", required=True)
    rollback.add_argument("--candidate-token-id", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--token-id", required=True)
    revoked = subparsers.add_parser("confirm-revoked")
    revoked.add_argument("--token-id", required=True)
    subparsers.add_parser("cycle")
    retire = subparsers.add_parser("retire-backup")
    retire.add_argument("--backup-id", required=True)
    retire.add_argument("--provider-credential-revoked", action="store_true")
    rehearse = subparsers.add_parser("rehearse")
    rehearse.add_argument("--fixture-root", required=True, type=Path)
    rehearse.add_argument("--inject-after-env", action="store_true")
    return parser


def main() -> int:
    """Run one operator command and print only a secret-free JSON receipt."""
    arguments = _parser().parse_args()

    def interrupted(_signum: int, _frame: object) -> None:
        raise CutoverError("operator_command_interrupted")

    prior_handlers = {
        signum: signal.signal(signum, interrupted)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
    }
    try:
        if arguments.command == "rehearse":
            receipt = _rehearse(
                arguments.fixture_root,
                inject_after_env=arguments.inject_after_env,
            )
        else:
            if os.geteuid() != 0:
                raise CutoverError("root_required")
            settings = Settings.production()
            if arguments.command == "preflight":
                receipt = _preflight(settings)
            elif arguments.command == "recover":
                receipt = _recover(settings)
            elif arguments.command == "rollback":
                receipt = _rollback(settings, arguments.backup_id, arguments.candidate_token_id)
            elif arguments.command == "verify":
                receipt = {
                    "status": "PASS",
                    "operation": "verify",
                    "live": _verify_live(settings, arguments.token_id),
                }
            elif arguments.command == "confirm-revoked":
                token_id = _validated_uuid(arguments.token_id, "downstream_token_id")
                if _token_item(settings, token_id).get("revoked_at") is None:
                    raise CutoverError("downstream_token_revoke_unconfirmed")
                receipt = {
                    "status": "PASS",
                    "operation": "confirm-revoked",
                    "token_id": token_id,
                    "revoked_confirmed": True,
                }
            elif arguments.command == "cycle":
                receipt = _cycle(settings)
            elif arguments.command == "retire-backup":
                receipt = _retire_backup(
                    settings,
                    arguments.backup_id,
                    provider_credential_revoked=arguments.provider_credential_revoked,
                )
            else:
                raise CutoverError("unknown_command")
    except CutoverError as error:
        failure: dict[str, object] = {"status": "FAIL", "error": str(error)}
        if "recovery_required" in str(error):
            failure["next_action"] = "run_recover"
        print(json.dumps(failure, separators=(",", ":")))
        return 1
    except Exception:
        print(json.dumps({"status": "FAIL", "error": "unexpected_runtime_error"}))
        return 1
    finally:
        for signum, handler in prior_handlers.items():
            _ = signal.signal(signum, handler)
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0 if receipt.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
