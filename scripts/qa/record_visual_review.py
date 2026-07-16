"""Record one artifact-bound visual-review LGTM without inventing review evidence."""

import argparse
import hashlib
import os
import re
import stat
import sys
from pathlib import Path
from typing import Annotated, ClassVar, Literal, final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

_SHA256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_IMAGE_DIGEST = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_READ_CHUNK_BYTES = 1024 * 1024
type _Lane = Literal["objective-visual", "design-accessibility-persona"]


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class _RunArtifactHashes(_StrictModel):
    adversarial: _SHA256
    candidate: _SHA256
    capture_index: _SHA256
    lighthouse: _SHA256
    manual_qa: _SHA256
    stack_cleanup: _SHA256


class _RunArtifactBinding(_StrictModel):
    run_a: _RunArtifactHashes = Field(alias="run-a")
    run_b: _RunArtifactHashes = Field(alias="run-b")


class _ReviewRequest(_StrictModel):
    fresh_cleanup_sha256: None
    image_digest: _IMAGE_DIGEST
    postgres_image_digest: _IMAGE_DIGEST
    process_baseline_sha256: _SHA256
    run_artifact_sha256: _RunArtifactBinding
    schema_version: Literal[2]
    source_tree_sha256: _SHA256
    status: Literal["REVIEW_REQUIRED"]


class _SourceManifest(_StrictModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow", frozen=True)

    source_tree_sha256: _SHA256


class _CleanupRemaining(_StrictModel):
    browser_processes: Literal[0]
    browser_temporary_directories: Literal[0]
    containers: Literal[0]
    lighthouse_processes: Literal[0]
    networks: Literal[0]
    playwright_drivers: Literal[0]
    port_listeners: Literal[0]
    temp_client_directories: Literal[0]
    temp_secret_directories: Literal[0]
    temporary_postgres_images: Literal[0]
    volumes: Literal[0]


class _CleanupReceipt(_StrictModel):
    final_exit_status: Literal[75]
    remaining: _CleanupRemaining
    schema_version: Literal[1]
    status: Literal["PASS"]
    trigger_exit_status: Literal[75]


class _CaptureRecord(_StrictModel):
    name: str
    route: str
    state: str
    viewport: str
    reduced_motion: bool
    native_zoom: bool
    path: str
    sha256: _SHA256
    byte_count: int = Field(gt=0)
    source_newest_mtime_ns: int
    capture_mtime_ns: int
    source_paths: tuple[str, ...]
    pixel_width: int
    pixel_height: int
    landmarks: tuple[str, ...]
    content_row_coverage: float


class _CaptureIndex(_StrictModel):
    captures: tuple[_CaptureRecord, ...]


class _VisualReview(_StrictModel):
    blocking_findings: tuple[str, ...] = ()
    fresh_cleanup_sha256: _SHA256
    image_digest: _IMAGE_DIGEST
    postgres_image_digest: _IMAGE_DIGEST
    lane: _Lane
    process_baseline_sha256: _SHA256
    review_request_sha256: _SHA256
    run_artifact_sha256: _RunArtifactBinding
    schema_version: Literal[2] = 2
    source_tree_sha256: _SHA256
    status: Literal["PASS"] = "PASS"


@final
class _Arguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.evidence_dir = Path()
        self.lane: _Lane = "objective-visual"
        self.review_request_sha256 = ""
        self.verdict: Literal["lgtm"] = "lgtm"


def _arguments() -> _Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--evidence-dir", type=Path, required=True)
    _ = parser.add_argument(
        "--lane",
        choices=("objective-visual", "design-accessibility-persona"),
        required=True,
    )
    _ = parser.add_argument("--review-request-sha256", required=True)
    _ = parser.add_argument("--verdict", choices=("lgtm",), required=True)
    arguments = _Arguments()
    _ = parser.parse_args(namespace=arguments)
    return arguments


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


def _metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular(path: Path) -> bytes:
    parent = _open_directory_no_follow(path.parent)
    descriptor = -1
    check_descriptor = -1
    try:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        check_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        current = os.fstat(check_descriptor)
        if _metadata(before) != _metadata(after) or _metadata(after) != _metadata(current):
            raise ValueError
        return b"".join(chunks)
    finally:
        if check_descriptor >= 0:
            os.close(check_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _sha256(path: Path) -> str:
    return hashlib.sha256(_read_regular(path)).hexdigest()


def _directory_entries(path: Path) -> set[str]:
    descriptor = _open_directory_no_follow(path)
    try:
        return set(os.listdir(descriptor))  # noqa: PTH208 - descriptor prevents symlink drift.
    finally:
        os.close(descriptor)


def _verify_run(root: Path, name: Literal["run-a", "run-b"], expected: _RunArtifactHashes) -> None:
    run = root / "runs" / name
    files = {
        "adversarial": "adversarial.json",
        "candidate": "candidate.json",
        "capture_index": "capture-index.json",
        "lighthouse": "lighthouse.json",
        "manual_qa": "manual-qa.json",
        "stack_cleanup": "stack-cleanup.json",
    }
    for field, filename in files.items():
        if _sha256(run / filename) != getattr(expected, field):
            raise ValueError
    index = _CaptureIndex.model_validate_json(_read_regular(run / "capture-index.json"))
    names = tuple(item.name for item in index.captures)
    if len(names) != len(set(names)) or any(Path(item).name != item for item in names):
        raise ValueError
    expected_files = {f"{item}.png" for item in names}
    if _directory_entries(run / "captures") != expected_files:
        raise ValueError
    for item in index.captures:
        expected_path = (run / "captures" / f"{item.name}.png").absolute()
        if Path(item.path) != expected_path:
            raise ValueError
        content = _read_regular(expected_path)
        if len(content) != item.byte_count or hashlib.sha256(content).hexdigest() != item.sha256:
            raise ValueError


def _write_exclusive(directory: Path, filename: str, payload: bytes) -> None:
    parent = _open_directory_no_follow(directory)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        created = True
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            _require_positive_write(count)
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.fsync(parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            os.unlink(filename, dir_fd=parent)
        raise
    finally:
        os.close(parent)


def _require_positive_write(count: int) -> None:
    if count <= 0:
        raise OSError


def _require(condition: bool) -> None:
    if not condition:
        raise ValueError


def _record(args: _Arguments) -> None:
    _require(_SHA_PATTERN.fullmatch(args.review_request_sha256) is not None)
    root = args.evidence_dir.absolute()
    request_bytes = _read_regular(root / "review-request.json")
    _require(hashlib.sha256(request_bytes).hexdigest() == args.review_request_sha256)
    request = _ReviewRequest.model_validate_json(request_bytes)
    source = _SourceManifest.model_validate_json(_read_regular(root / "source-manifest.json"))
    _require(source.source_tree_sha256 == request.source_tree_sha256)
    _require(_sha256(root / "process-baseline.json") == request.process_baseline_sha256)
    cleanup_bytes = _read_regular(root / "cleanup-fresh.json")
    _ = _CleanupReceipt.model_validate_json(cleanup_bytes)
    cleanup_sha = hashlib.sha256(cleanup_bytes).hexdigest()
    _verify_run(root, "run-a", request.run_artifact_sha256.run_a)
    _verify_run(root, "run-b", request.run_artifact_sha256.run_b)
    review = _VisualReview(
        fresh_cleanup_sha256=cleanup_sha,
        image_digest=request.image_digest,
        postgres_image_digest=request.postgres_image_digest,
        lane=args.lane,
        process_baseline_sha256=request.process_baseline_sha256,
        review_request_sha256=args.review_request_sha256,
        run_artifact_sha256=request.run_artifact_sha256,
        source_tree_sha256=request.source_tree_sha256,
    )
    filename = "visual-review-a.json" if args.lane == "objective-visual" else "visual-review-b.json"
    payload = (review.model_dump_json(indent=2, by_alias=True) + "\n").encode()
    _write_exclusive(root, filename, payload)


def main() -> int:
    """Validate the exact review request and exclusively record one LGTM lane."""
    try:
        _record(_arguments())
    except Exception:  # noqa: BLE001 - closed process boundary never emits artifact data.
        _ = sys.stderr.write("visual_review_receipt_failed\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
