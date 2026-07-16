import argparse
import hashlib
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, final

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from .browser_evidence import AdversarialReceipt, CaptureIndex
from .browser_prod_models import (
    BrowserNetworkProjection,
    NativeStableProjection,
    ProductionBrowserReceipt,
    ProductionRunCandidate,
)
from .lighthouse_gate import LighthouseReceipt

_UUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_READ_CHUNK_BYTES: Final = 1024 * 1024

_EXPECTED_CAPTURE_COUNT: Final = 35
_EXPECTED_SCENARIO_COUNT: Final = 10
_EXPECTED_ADVERSARIAL_PROBE_COUNT: Final = 5
_EXPECTED_LIGHTHOUSE_AUDIT_COUNT: Final = 4
_EXPECTED_NATIVE_STABLE_COUNT: Final = 6
_EXPECTED_NATIVE_CAPTURE_COUNT: Final = 10


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class _RunArtifactHashes(_StrictModel):
    adversarial: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    candidate: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    capture_index: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    lighthouse: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    manual_qa: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    stack_cleanup: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


type _RunArtifactBinding = dict[Literal["run-a", "run-b"], _RunArtifactHashes]


@final
class _Arguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.evidence_dir = Path()
        self.source_sha = ""
        self.image_digest = ""
        self.postgres_image_digest = ""


class _VisualReview(_StrictModel):
    blocking_findings: tuple[str, ...]
    fresh_cleanup_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    image_digest: str
    postgres_image_digest: str
    lane: Literal["objective-visual", "design-accessibility-persona"]
    process_baseline_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    review_request_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    run_artifact_sha256: _RunArtifactBinding
    schema_version: Literal[2]
    source_tree_sha256: str
    status: Literal["PASS"]


class _ReviewRequest(_StrictModel):
    fresh_cleanup_sha256: None
    image_digest: str
    postgres_image_digest: str
    process_baseline_sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    run_artifact_sha256: _RunArtifactBinding
    schema_version: Literal[2]
    source_tree_sha256: str
    status: Literal["REVIEW_REQUIRED"]


class _DeterminismComparisons(_StrictModel):
    capture_projection_exact: Literal[True]
    lighthouse_medians_exact: Literal[True]
    manual_projection_exact: Literal[True]
    source_and_image_binding_exact: Literal[True]


class _ShellCleanupRemaining(_StrictModel):
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


class _FreshCleanupReceipt(_StrictModel):
    final_exit_status: Literal[75]
    remaining: _ShellCleanupRemaining
    schema_version: Literal[1]
    status: Literal["PASS"]
    trigger_exit_status: Literal[75]


class _DeterminismReceipt(_StrictModel):
    comparisons: _DeterminismComparisons
    image_digest: str
    postgres_image_digest: str
    runs: tuple[Literal["runs/run-a"], Literal["runs/run-b"]]
    schema_version: Literal[1]
    source_tree_sha256: str
    status: Literal["PASS"]


class _CaptureProjection(_StrictModel):
    name: str
    route: str
    state: str
    viewport: str
    reduced_motion: bool
    native_zoom: bool
    source_paths: tuple[str, ...]
    pixel_width: int
    pixel_height: int
    landmarks: tuple[str, ...]
    content_row_coverage: float


class _LighthouseProjection(_StrictModel):
    route: Literal["admin", "showcase"]
    form_factor: Literal["mobile", "desktop"]
    median_scores: dict[str, int]


class _VisualReviewStates(_StrictModel):
    pass_a: bool
    pass_b: bool


class _CandidateReceipt(_StrictModel):
    capture_count_per_run: int
    image_digest: str
    postgres_image_digest: str
    lighthouse_medians: tuple[_LighthouseProjection, ...]
    schema_version: Literal[1]
    source_tree_sha256: str
    status: Literal["PASS", "REVIEW_REQUIRED"]
    visual_reviews: _VisualReviewStates


class _RunStackCleanupRemaining(_StrictModel):
    browser_processes: Literal[0]
    browser_temporary_directories: Literal[0]
    containers: Literal[0]
    lighthouse_processes: Literal[0]
    networks: Literal[0]
    playwright_drivers: Literal[0]
    port_listeners: Literal[0]
    temporary_postgres_images: Literal[0]
    volumes: Literal[0]


class _RunStackCleanupReceipt(_StrictModel):
    remaining: _RunStackCleanupRemaining
    run: Literal["run-a", "run-b"]
    schema_version: Literal[1]
    status: Literal["PASS"]


@dataclass(frozen=True, slots=True)
class _StableProjection:
    label: str
    dom_hash: str
    focus_stops: int
    axe_serious: int
    axe_critical: int
    axe_network_requests: int
    focus_clipped: int
    focus_hidden: int
    focus_covered: int


@dataclass(frozen=True, slots=True)
class _ManualProjection:
    signature: tuple[object, ...]
    native_stable: tuple[_StableProjection, ...]
    network: tuple[BrowserNetworkProjection, ...]


@dataclass(frozen=True, slots=True)
class _RunEvidence:
    run_name: Literal["run-a", "run-b"]
    manual: ProductionBrowserReceipt
    capture: CaptureIndex
    lighthouse: LighthouseReceipt
    adversarial: AdversarialReceipt
    candidate: ProductionRunCandidate
    stack_cleanup: _RunStackCleanupReceipt
    source_sha: str
    image_digest: str


def _arguments() -> _Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--evidence-dir", type=Path, required=True)
    _ = parser.add_argument("--source-sha", required=True)
    _ = parser.add_argument("--image-digest", required=True)
    _ = parser.add_argument("--postgres-image-digest", required=True)
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


def _stable_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_regular_bytes(path: Path) -> bytes:
    if not path.name or path.name in {".", ".."}:
        reason = "evidence path is not a closed file path"
        raise AssertionError(reason)
    parent = _open_directory_no_follow(path.parent)
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    check_descriptor = -1
    try:
        descriptor = os.open(path.name, file_flags, dir_fd=parent)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            reason = "evidence artifact is not a regular file"
            raise AssertionError(reason)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        check_descriptor = os.open(path.name, file_flags, dir_fd=parent)
        current = os.fstat(check_descriptor)
        if _stable_metadata(before) != _stable_metadata(after) or _stable_metadata(
            after
        ) != _stable_metadata(current):
            reason = "evidence artifact changed during verification"
            raise AssertionError(reason)
        return b"".join(chunks)
    finally:
        if check_descriptor >= 0:
            os.close(check_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _directory_entries(path: Path) -> set[str]:
    descriptor = _open_directory_no_follow(path)
    try:
        return set(os.listdir(descriptor))  # noqa: PTH208 - descriptor prevents symlink traversal.
    finally:
        os.close(descriptor)


def _write(path: Path, value: BaseModel) -> None:
    payload = (value.model_dump_json(indent=2) + "\n").encode()
    parent = _open_directory_no_follow(path.parent)
    temporary_name = f".{path.name}.{os.getpid()}.{id(value)}.tmp"
    created = False
    descriptor = -1
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        created = True
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                _raise_write_no_progress()
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary_name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except BaseException:
        if created:
            with suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=parent)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent)


def _write_exclusive(path: Path, value: BaseModel) -> bytes:
    payload = (value.model_dump_json(indent=2) + "\n").encode()
    parent = _open_directory_no_follow(path.parent)
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        created = True
        written = 0
        while written < len(payload):
            count = os.write(descriptor, payload[written:])
            if count <= 0:
                _raise_write_no_progress()
            written += count
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.fsync(parent)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            with suppress(FileNotFoundError):
                os.unlink(path.name, dir_fd=parent)
        raise
    finally:
        os.close(parent)
    return payload


def _immutable_review_request(
    root: Path,
    expected: _ReviewRequest,
    *,
    resume: bool,
) -> tuple[_ReviewRequest, str]:
    path = root / "review-request.json"
    if resume:
        payload = _read_regular_bytes(path)
        observed = _ReviewRequest.model_validate_json(payload)
        _assert_exact(observed, expected, "review request semantics changed")
    else:
        payload = _write_exclusive(path, expected)
        observed = expected
    return observed, hashlib.sha256(payload).hexdigest()


def _raise_write_no_progress() -> None:
    reason = "evidence receipt write made no progress"
    raise OSError(reason)


def _normalized_network(
    manual: ProductionBrowserReceipt,
) -> tuple[BrowserNetworkProjection, ...]:
    normalized = tuple(
        item.model_copy(update={"path": _UUID.sub("{uuid}", item.path)}) for item in manual.network
    )
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.phase,
                item.method,
                item.path,
                -1 if item.status is None else item.status,
                item.failed,
                item.query_present,
            ),
        )
    )


def _stable_projection(item: NativeStableProjection) -> _StableProjection:
    return _StableProjection(
        label=item.label,
        dom_hash=item.dom_hash,
        focus_stops=item.focus_stops,
        axe_serious=item.axe_serious,
        axe_critical=item.axe_critical,
        axe_network_requests=item.axe_network_requests,
        focus_clipped=item.focus_clipped,
        focus_hidden=item.focus_hidden,
        focus_covered=item.focus_covered,
    )


def _manual_projection(manual: ProductionBrowserReceipt) -> _ManualProjection:
    native = manual.native
    signature: tuple[object, ...] = (
        manual.provenance,
        manual.scenarios,
        manual.axe_serious,
        manual.axe_critical,
        manual.axe_network_requests,
        manual.console_errors,
        manual.page_errors,
        manual.reduced_motion,
        manual.admin_desktop,
        manual.showcase_desktop,
        manual.network_query_count,
        manual.network_header_body_count,
        manual.admin_bearer_dom_absent,
        manual.one_time_token_dom_absent,
        manual.ordinary_cleanup,
        native.chromium_revision,
        native.contexts_started,
        native.pages_started,
        native.maximum_live_contexts,
        native.maximum_live_pages,
        native.shared_browser_used,
        native.executable_path,
        native.preference_sha256,
        native.physical_width,
        native.physical_height,
        native.css_viewport_width,
        native.css_viewport_height,
        native.scroll_width,
        native.observation,
        native.capture_ids,
        native.cleanup,
    )
    return _ManualProjection(
        signature=signature,
        native_stable=tuple(_stable_projection(item) for item in native.stable),
        network=_normalized_network(manual),
    )


def _capture_projection(capture: CaptureIndex) -> tuple[_CaptureProjection, ...]:
    return tuple(
        _CaptureProjection(
            name=row.name,
            route=row.route,
            state=row.state,
            viewport=row.viewport,
            reduced_motion=row.reduced_motion,
            native_zoom=row.native_zoom,
            source_paths=row.source_paths,
            pixel_width=row.pixel_width,
            pixel_height=row.pixel_height,
            landmarks=row.landmarks,
            content_row_coverage=row.content_row_coverage,
        )
        for row in capture.captures
    )


def _lighthouse_projection(
    receipt: LighthouseReceipt,
) -> tuple[_LighthouseProjection, ...]:
    return tuple(
        _LighthouseProjection(
            route=item.route,
            form_factor=item.form_factor,
            median_scores=item.median_scores,
        )
        for item in receipt.audits
    )


def _review_is_valid(path: Path, expected: _VisualReview) -> bool:
    try:
        observed = _VisualReview.model_validate_json(_read_regular_bytes(path))
    except (AssertionError, OSError, ValidationError):
        return False
    return observed == expected and not observed.blocking_findings


def _fresh_cleanup_binding(root: Path) -> str | None:
    try:
        payload = _read_regular_bytes(root / "cleanup-fresh.json")
    except FileNotFoundError:
        return None
    _ = _FreshCleanupReceipt.model_validate_json(payload)
    return hashlib.sha256(payload).hexdigest()


def _verified_capture_index(run: Path) -> tuple[CaptureIndex, str]:
    index_bytes = _read_regular_bytes(run / "capture-index.json")
    capture = CaptureIndex.model_validate_json(index_bytes)
    capture_directory = run / "captures"
    names = tuple(record.name for record in capture.captures)
    if len(names) != len(set(names)) or any(
        not name or name in {".", ".."} or Path(name).name != name for name in names
    ):
        reason = "capture index contains duplicate or non-leaf names"
        raise AssertionError(reason)
    expected_files = {f"{name}.png" for name in names}
    observed_entries = _directory_entries(capture_directory)
    if observed_entries != expected_files:
        reason = "capture directory entry set changed after capture"
        raise AssertionError(reason)
    for record in capture.captures:
        expected = (capture_directory / f"{record.name}.png").absolute()
        observed = Path(record.path)
        if not observed.is_absolute() or observed != expected:
            reason = "capture path is not bound to its run capture directory"
            raise AssertionError(reason)
        content = _read_regular_bytes(observed)
        if (
            len(content) != record.byte_count
            or hashlib.sha256(content).hexdigest() != record.sha256
        ):
            reason = "capture bytes changed after capture index creation"
            raise AssertionError(reason)
    if _directory_entries(capture_directory) != expected_files:
        reason = "capture directory entry set changed during verification"
        raise AssertionError(reason)
    return capture, hashlib.sha256(index_bytes).hexdigest()


def _run_artifact_hashes(run: Path, capture_index_sha256: str) -> _RunArtifactHashes:
    return _RunArtifactHashes(
        adversarial=hashlib.sha256(_read_regular_bytes(run / "adversarial.json")).hexdigest(),
        candidate=hashlib.sha256(_read_regular_bytes(run / "candidate.json")).hexdigest(),
        capture_index=capture_index_sha256,
        lighthouse=hashlib.sha256(_read_regular_bytes(run / "lighthouse.json")).hexdigest(),
        manual_qa=hashlib.sha256(_read_regular_bytes(run / "manual-qa.json")).hexdigest(),
        stack_cleanup=hashlib.sha256(_read_regular_bytes(run / "stack-cleanup.json")).hexdigest(),
    )


def _assert_run_receipts(evidence: _RunEvidence) -> None:
    run_name = evidence.run_name
    manual = evidence.manual
    capture = evidence.capture
    lighthouse = evidence.lighthouse
    adversarial = evidence.adversarial
    candidate = evidence.candidate
    stack_cleanup = evidence.stack_cleanup
    source_sha = evidence.source_sha
    image_digest = evidence.image_digest
    _assert_exact(manual.run_name, run_name, "manual run identity changed")
    _assert_exact(candidate.run_name, run_name, "candidate run identity changed")
    _assert_exact(stack_cleanup.run, run_name, "cleanup run identity changed")
    _assert_exact(manual.source_tree_sha256, source_sha, "manual source binding changed")
    _assert_exact(candidate.source_tree_sha256, source_sha, "candidate source binding changed")
    _assert_exact(manual.image_digest, image_digest, "manual image binding changed")
    _assert_exact(candidate.image_digest, image_digest, "candidate image binding changed")
    _assert_exact(lighthouse.image_digest, image_digest, "Lighthouse image binding changed")
    _assert_exact(len(capture.captures), _EXPECTED_CAPTURE_COUNT, "capture count changed")
    _assert_exact(candidate.capture_count, _EXPECTED_CAPTURE_COUNT, "candidate count changed")
    _assert_exact(len(manual.scenarios), _EXPECTED_SCENARIO_COUNT, "scenario count changed")
    _assert_exact(
        len(adversarial.probes),
        _EXPECTED_ADVERSARIAL_PROBE_COUNT,
        "adversarial probe count changed",
    )
    _assert_exact(
        tuple(probe.status for probe in adversarial.probes),
        ("passed",) * _EXPECTED_ADVERSARIAL_PROBE_COUNT,
        "adversarial probe status changed",
    )
    _assert_exact(
        len(lighthouse.audits),
        _EXPECTED_LIGHTHOUSE_AUDIT_COUNT,
        "Lighthouse audit count changed",
    )
    _assert_exact(
        tuple((audit.route, audit.form_factor) for audit in lighthouse.audits),
        (
            ("admin", "mobile"),
            ("admin", "desktop"),
            ("showcase", "mobile"),
            ("showcase", "desktop"),
        ),
        "Lighthouse audit tuple changed",
    )
    _assert_exact(
        len(manual.native.stable),
        _EXPECTED_NATIVE_STABLE_COUNT,
        "native stable projection count changed",
    )
    _assert_exact(
        len(manual.native.capture_ids),
        _EXPECTED_NATIVE_CAPTURE_COUNT,
        "native capture identity count changed",
    )


def _assert_exact(left: object, right: object, reason: str) -> None:
    if left != right:
        raise AssertionError(reason)


def main() -> int:
    args = _arguments()
    root = args.evidence_dir
    run_a = root / "runs" / "run-a"
    run_b = root / "runs" / "run-b"
    _ = _directory_entries(root)
    fresh_cleanup_sha = _fresh_cleanup_binding(root)
    manual_a = ProductionBrowserReceipt.model_validate_json(
        _read_regular_bytes(run_a / "manual-qa.json")
    )
    manual_b = ProductionBrowserReceipt.model_validate_json(
        _read_regular_bytes(run_b / "manual-qa.json")
    )
    capture_a, capture_index_sha_a = _verified_capture_index(run_a)
    capture_b, capture_index_sha_b = _verified_capture_index(run_b)
    process_baseline_sha = hashlib.sha256(
        _read_regular_bytes(root / "process-baseline.json")
    ).hexdigest()
    lighthouse_a = LighthouseReceipt.model_validate_json(
        _read_regular_bytes(run_a / "lighthouse.json")
    )
    lighthouse_b = LighthouseReceipt.model_validate_json(
        _read_regular_bytes(run_b / "lighthouse.json")
    )
    adversarial_a = AdversarialReceipt.model_validate_json(
        _read_regular_bytes(run_a / "adversarial.json")
    )
    adversarial_b = AdversarialReceipt.model_validate_json(
        _read_regular_bytes(run_b / "adversarial.json")
    )
    candidate_a = ProductionRunCandidate.model_validate_json(
        _read_regular_bytes(run_a / "candidate.json")
    )
    candidate_b = ProductionRunCandidate.model_validate_json(
        _read_regular_bytes(run_b / "candidate.json")
    )
    stack_cleanup_a = _RunStackCleanupReceipt.model_validate_json(
        _read_regular_bytes(run_a / "stack-cleanup.json")
    )
    stack_cleanup_b = _RunStackCleanupReceipt.model_validate_json(
        _read_regular_bytes(run_b / "stack-cleanup.json")
    )
    _assert_run_receipts(
        _RunEvidence(
            run_name="run-a",
            manual=manual_a,
            capture=capture_a,
            lighthouse=lighthouse_a,
            adversarial=adversarial_a,
            candidate=candidate_a,
            stack_cleanup=stack_cleanup_a,
            source_sha=args.source_sha,
            image_digest=args.image_digest,
        )
    )
    _assert_run_receipts(
        _RunEvidence(
            run_name="run-b",
            manual=manual_b,
            capture=capture_b,
            lighthouse=lighthouse_b,
            adversarial=adversarial_b,
            candidate=candidate_b,
            stack_cleanup=stack_cleanup_b,
            source_sha=args.source_sha,
            image_digest=args.image_digest,
        )
    )
    _assert_exact(adversarial_a, adversarial_b, "adversarial receipt tuple changed")
    run_artifact_binding: _RunArtifactBinding = {
        "run-a": _run_artifact_hashes(run_a, capture_index_sha_a),
        "run-b": _run_artifact_hashes(run_b, capture_index_sha_b),
    }
    manual_projection_a = _manual_projection(manual_a)
    capture_projection_a = _capture_projection(capture_a)
    lighthouse_projection_a = _lighthouse_projection(lighthouse_a)
    manual_projection_b = _manual_projection(manual_b)
    _assert_exact(
        manual_projection_a.signature,
        manual_projection_b.signature,
        "browser deterministic manual signature changed",
    )
    _assert_exact(
        manual_projection_a.native_stable,
        manual_projection_b.native_stable,
        "browser deterministic native stable projection changed",
    )
    _assert_exact(
        manual_projection_a.network,
        manual_projection_b.network,
        "browser deterministic network projection changed",
    )
    _assert_exact(
        capture_projection_a,
        _capture_projection(capture_b),
        "browser deterministic capture projection changed",
    )
    _assert_exact(
        lighthouse_projection_a,
        _lighthouse_projection(lighthouse_b),
        "Lighthouse deterministic median projection changed",
    )
    expected_review_request = _ReviewRequest(
        fresh_cleanup_sha256=None,
        image_digest=args.image_digest,
        postgres_image_digest=args.postgres_image_digest,
        process_baseline_sha256=process_baseline_sha,
        run_artifact_sha256=run_artifact_binding,
        schema_version=2,
        source_tree_sha256=args.source_sha,
        status="REVIEW_REQUIRED",
    )
    review_request, review_request_sha = _immutable_review_request(
        root,
        expected_review_request,
        resume=fresh_cleanup_sha is not None,
    )
    review_binding = review_request.run_artifact_sha256
    _write(
        root / "determinism.json",
        _DeterminismReceipt(
            comparisons=_DeterminismComparisons(
                capture_projection_exact=True,
                lighthouse_medians_exact=True,
                manual_projection_exact=True,
                source_and_image_binding_exact=True,
            ),
            image_digest=args.image_digest,
            postgres_image_digest=args.postgres_image_digest,
            runs=("runs/run-a", "runs/run-b"),
            schema_version=1,
            source_tree_sha256=args.source_sha,
            status="PASS",
        ),
    )
    review_a = fresh_cleanup_sha is not None and _review_is_valid(
        root / "visual-review-a.json",
        _VisualReview(
            blocking_findings=(),
            fresh_cleanup_sha256=fresh_cleanup_sha,
            image_digest=args.image_digest,
            postgres_image_digest=args.postgres_image_digest,
            lane="objective-visual",
            process_baseline_sha256=process_baseline_sha,
            review_request_sha256=review_request_sha,
            run_artifact_sha256=review_binding,
            schema_version=2,
            source_tree_sha256=args.source_sha,
            status="PASS",
        ),
    )
    review_b = fresh_cleanup_sha is not None and _review_is_valid(
        root / "visual-review-b.json",
        _VisualReview(
            blocking_findings=(),
            fresh_cleanup_sha256=fresh_cleanup_sha,
            image_digest=args.image_digest,
            postgres_image_digest=args.postgres_image_digest,
            lane="design-accessibility-persona",
            process_baseline_sha256=process_baseline_sha,
            review_request_sha256=review_request_sha,
            run_artifact_sha256=review_binding,
            schema_version=2,
            source_tree_sha256=args.source_sha,
            status="PASS",
        ),
    )
    final_status: Literal["PASS", "REVIEW_REQUIRED"] = (
        "PASS" if review_a and review_b else "REVIEW_REQUIRED"
    )
    _write(
        root / "candidate.json",
        _CandidateReceipt(
            capture_count_per_run=len(capture_projection_a),
            image_digest=args.image_digest,
            postgres_image_digest=args.postgres_image_digest,
            lighthouse_medians=lighthouse_projection_a,
            schema_version=1,
            source_tree_sha256=args.source_sha,
            status=final_status,
            visual_reviews=_VisualReviewStates(pass_a=review_a, pass_b=review_b),
        ),
    )
    _write(root / "manual-qa.json", manual_a)
    adversarial = AdversarialReceipt.model_validate_json(
        _read_regular_bytes(run_a / "adversarial.json")
    )
    _write(root / "adversarial.json", adversarial)
    if final_status != "PASS":
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
