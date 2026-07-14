"""Manifest-bound source snapshot isolation tests."""

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from scripts.qa import source_snapshot
from scripts.qa.source_manifest import CandidatePayload, SourceManifest, build_manifest
from scripts.qa.source_snapshot import create_snapshot


def _initialize_source(root: Path) -> Path:
    git = shutil.which("git")
    assert git is not None
    _ = subprocess.run([git, "init", "-q"], cwd=root, check=True)  # noqa: S603
    ignore = root / ".gitignore"
    _ = ignore.write_text(".omo/\n", encoding="utf-8")
    ignore.chmod(0o644)
    package = root / "package"
    package.mkdir()
    candidate = package / "candidate.sh"
    _ = candidate.write_text("#!/bin/sh\nprintf snapshot\\n\n", encoding="utf-8")
    candidate.chmod(0o755)
    _ = (root / "candidate-link").symlink_to("package/candidate.sh")
    return candidate


def _write_manifest(root: Path, path: Path) -> SourceManifest:
    manifest = build_manifest(root)
    _ = path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def test_source_snapshot_copies_exact_manifest_and_freezes_regular_files(tmp_path: Path) -> None:
    candidate = _initialize_source(tmp_path)
    manifest_path = tmp_path.parent / f"{tmp_path.name}-manifest.json"
    output = tmp_path.parent / f"{tmp_path.name}-snapshot"
    manifest = _write_manifest(tmp_path, manifest_path)

    try:
        receipt = create_snapshot(tmp_path, manifest_path, output)

        expected_paths = {entry["path"] for entry in manifest["entries"]}
        observed_paths = {
            path.relative_to(output).as_posix() for path in output.rglob("*") if not path.is_dir()
        }
        assert observed_paths == expected_paths
        assert (output / "package/candidate.sh").read_bytes() == candidate.read_bytes()
        assert (output / "candidate-link").is_symlink()
        assert (output / "candidate-link").readlink() == Path("package/candidate.sh")
        assert stat.S_IMODE((output / "package/candidate.sh").stat().st_mode) == 0o555
        assert stat.S_IMODE((output / ".gitignore").stat().st_mode) == 0o444
        assert stat.S_IMODE((output / "package").stat().st_mode) == 0o555
        assert stat.S_IMODE(output.stat().st_mode) == 0o555
        assert receipt.entry_count == manifest["entry_count"]
        assert receipt.source_tree_sha256 == manifest["source_tree_sha256"]
        assert receipt.writable_regular_files == 0
    finally:
        manifest_path.unlink(missing_ok=True)
        for path in (output, *output.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        if output.exists():
            shutil.rmtree(output)


def test_source_snapshot_rejects_source_drift_before_creating_output(tmp_path: Path) -> None:
    candidate = _initialize_source(tmp_path)
    manifest_path = tmp_path.parent / f"{tmp_path.name}-manifest.json"
    output = tmp_path.parent / f"{tmp_path.name}-snapshot"
    _ = _write_manifest(tmp_path, manifest_path)
    _ = candidate.write_text("changed after manifest\n", encoding="utf-8")

    try:
        with pytest.raises(ValueError, match="changed before snapshot"):
            _ = create_snapshot(tmp_path, manifest_path, output)
        assert not output.exists()
    finally:
        manifest_path.unlink(missing_ok=True)


def test_source_snapshot_rejects_injected_extra_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = _initialize_source(tmp_path)
    manifest_path = tmp_path.parent / f"{tmp_path.name}-manifest.json"
    output = tmp_path.parent / f"{tmp_path.name}-snapshot"
    _ = _write_manifest(tmp_path, manifest_path)
    original = source_snapshot._materialize_entries  # pyright: ignore[reportPrivateUsage]

    def inject_extra(
        root: Path,
        destination: Path,
        entries: tuple[tuple[source_snapshot.SnapshotEntry, Path], ...],
    ) -> tuple[int, int]:
        result = original(root, destination, entries)
        rogue = destination / "rogue.txt"
        _ = rogue.write_text("not in manifest\n", encoding="utf-8")
        rogue.chmod(0o666)
        return result

    monkeypatch.setattr(source_snapshot, "_materialize_entries", inject_extra)
    try:
        with pytest.raises(ValueError, match="missing or extra path"):
            _ = create_snapshot(tmp_path, manifest_path, output)
        assert not output.exists()
    finally:
        manifest_path.unlink(missing_ok=True)


def test_source_snapshot_destination_never_follows_injected_ancestor_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = _initialize_source(tmp_path)
    manifest_path = tmp_path.parent / f"{tmp_path.name}-manifest.json"
    output = tmp_path.parent / f"{tmp_path.name}-snapshot"
    outside = tmp_path.parent / f"{tmp_path.name}-destination-outside"
    outside.mkdir()
    _ = _write_manifest(tmp_path, manifest_path)
    original = source_snapshot._create_snapshot_leaf  # pyright: ignore[reportPrivateUsage]

    def inject_destination_symlink(
        destination: Path,
        relative: Path,
        entry: source_snapshot.SnapshotEntry,
        payload: CandidatePayload,
    ) -> None:
        if relative == Path("package/candidate.sh"):
            _ = (destination / "package").symlink_to(outside, target_is_directory=True)
        original(destination, relative, entry, payload)

    monkeypatch.setattr(source_snapshot, "_create_snapshot_leaf", inject_destination_symlink)
    try:
        with pytest.raises(OSError, match=r"(Too many levels|Not a directory)"):
            _ = create_snapshot(tmp_path, manifest_path, output)
        assert not (outside / "candidate.sh").exists()
        assert not output.exists()
    finally:
        manifest_path.unlink(missing_ok=True)
        outside.rmdir()


def test_source_snapshot_rejects_escaping_symlink_without_partial_output(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    _ = subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)  # noqa: S603
    _ = (tmp_path / "escape").symlink_to("../outside")
    manifest_path = tmp_path.parent / f"{tmp_path.name}-manifest.json"
    output = tmp_path.parent / f"{tmp_path.name}-snapshot"
    _ = _write_manifest(tmp_path, manifest_path)

    try:
        with pytest.raises(ValueError, match="escapes the source root"):
            _ = create_snapshot(tmp_path, manifest_path, output)
        assert not output.exists()
    finally:
        manifest_path.unlink(missing_ok=True)


def test_source_manifest_rejects_symlinked_source_ancestor(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    _ = subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)  # noqa: S603
    nested = tmp_path / "nested"
    nested.mkdir()
    _ = (nested / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _ = subprocess.run(  # noqa: S603
        [git, "add", "nested/candidate.txt"], cwd=tmp_path, check=True
    )
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    _ = nested.rename(outside)
    _ = nested.symlink_to(outside, target_is_directory=True)

    try:
        with pytest.raises(OSError, match=r"(Too many levels|Not a directory)"):
            _ = build_manifest(tmp_path)
    finally:
        nested.unlink()
        _ = outside.rename(nested)
