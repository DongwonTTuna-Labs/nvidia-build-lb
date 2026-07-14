from os import environ
from pathlib import Path
from typing import Final, final, override

_OWNER_NAME: Final = ".nblb-ui-fake-owner"
_OWNER_VALUE: Final = "todo-4-ui-fake-v1\n"
_DEFAULT_EVIDENCE_DIRECTORY: Final = ".omo/evidence/task-4-nvidia-build-lb/runs/default"
_GATE_ROOT: Final = (
    Path(__file__).resolve().parents[2] / ".omo" / "evidence" / "task-4-nvidia-build-lb" / "runs"
)
_CLAIMED_DIRECTORIES: set[Path] = set()


@final
class EvidenceDirectoryError(Exception):
    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @override
    def __str__(self) -> str:
        return self.reason


def evidence_directory() -> Path:
    return Path(environ.get("EVIDENCE_DIR", _DEFAULT_EVIDENCE_DIRECTORY))


def claim_evidence_directory(directory: Path, gate_root: Path | None = None) -> Path:
    resolved = directory.resolve(strict=False)
    if directory.is_symlink():
        reason = "evidence leaf is a symlink"
        raise EvidenceDirectoryError(reason)
    allowed_root = (_GATE_ROOT if gate_root is None else gate_root).resolve(strict=False)
    if resolved.parent != allowed_root:
        reason = "evidence leaf is outside the Todo 4 gate root"
        raise EvidenceDirectoryError(reason)
    if resolved in _CLAIMED_DIRECTORIES:
        return directory
    owner = directory / _OWNER_NAME
    if directory.exists():
        reason = "evidence leaf is not fresh and absent"
        raise EvidenceDirectoryError(reason)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()
    _ = owner.write_text(_OWNER_VALUE, encoding="utf-8")
    _CLAIMED_DIRECTORIES.add(resolved)
    return directory


def reserve_capture_directory(directory: Path, gate_root: Path | None = None) -> Path:
    captures = claim_evidence_directory(directory, gate_root) / "captures"
    try:
        captures.mkdir()
    except FileExistsError as error:
        reason = "capture directory already exists; a fresh task-owned path is required"
        raise EvidenceDirectoryError(reason) from error
    return captures
