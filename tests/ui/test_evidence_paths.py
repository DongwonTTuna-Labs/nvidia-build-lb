from pathlib import Path

import pytest

from .browser_evidence import EvidenceRecorder
from .evidence_paths import EvidenceDirectoryError, claim_evidence_directory

pytestmark = pytest.mark.ui_fake


def _run_leaf(tmp_path: Path, name: str) -> Path:
    runs = tmp_path / "task-4-nvidia-build-lb" / "runs"
    runs.mkdir(parents=True)
    return runs / name


def test_existing_unowned_evidence_directory_is_preserved(tmp_path: Path) -> None:
    # Given: a caller-owned directory with an unrelated artifact.
    directory = _run_leaf(tmp_path, "caller-owned")
    directory.mkdir()
    artifact = directory / "preserve.txt"
    _ = artifact.write_text("caller-owned\n", encoding="utf-8")

    # When/Then: the UI harness refuses ownership without deleting caller data.
    with pytest.raises(EvidenceDirectoryError, match="not fresh"):
        _ = EvidenceRecorder(directory, directory.parent)
    assert artifact.read_text(encoding="utf-8") == "caller-owned\n"


def test_owned_evidence_directory_cannot_overwrite_existing_captures(tmp_path: Path) -> None:
    # Given: one newly claimed Todo 4 evidence root and capture directory.
    directory = _run_leaf(tmp_path, "todo-4")
    _ = EvidenceRecorder(directory, directory.parent)

    # When/Then: a second recorder cannot reuse or recursively delete its captures.
    with pytest.raises(EvidenceDirectoryError, match="already exists"):
        _ = EvidenceRecorder(directory, directory.parent)


def test_preexisting_owner_marker_cannot_claim_a_fresh_run(tmp_path: Path) -> None:
    # Given: an earlier process left only the known owner marker in a leaf.
    directory = _run_leaf(tmp_path, "stale-todo-4")
    directory.mkdir()
    _ = (directory / ".nblb-ui-fake-owner").write_text(
        "todo-4-ui-fake-v1\n",
        encoding="utf-8",
    )

    # When/Then: marker knowledge cannot turn a reused leaf into fresh evidence.
    with pytest.raises(EvidenceDirectoryError, match="fresh"):
        _ = claim_evidence_directory(directory, directory.parent)


def test_symlink_evidence_leaf_is_rejected_without_mutating_target(tmp_path: Path) -> None:
    # Given: a symlink points at a caller-owned directory with a convincing marker.
    target = tmp_path / "caller-target"
    target.mkdir()
    marker = target / ".nblb-ui-fake-owner"
    _ = marker.write_text("todo-4-ui-fake-v1\n", encoding="utf-8")
    leaf = _run_leaf(tmp_path, "linked-todo-4")
    leaf.symlink_to(target, target_is_directory=True)

    # When/Then: the leaf is refused and the caller marker remains unchanged.
    with pytest.raises(EvidenceDirectoryError, match="symlink"):
        _ = claim_evidence_directory(leaf, leaf.parent)
    assert marker.read_text(encoding="utf-8") == "todo-4-ui-fake-v1\n"


@pytest.mark.parametrize(
    "relative",
    ["task-6b-nvidia-build-lb/runs/run-a", "task-4-nvidia-build-lb/run-a", "arbitrary"],
)
def test_cross_gate_or_arbitrary_evidence_leaf_is_rejected(
    tmp_path: Path,
    relative: str,
) -> None:
    # Given: an absent path that is not one direct Todo 4 run leaf.
    directory = tmp_path / relative
    directory.parent.mkdir(parents=True, exist_ok=True)

    # When/Then: validation fails before the path is created.
    with pytest.raises(EvidenceDirectoryError, match=r"gate root|direct run"):
        _ = claim_evidence_directory(directory)
    assert not directory.exists()
