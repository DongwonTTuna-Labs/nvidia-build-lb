from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from sys import argv
from typing import ClassVar, final, override

from pydantic import BaseModel, ConfigDict

from .browser_evidence import CaptureIndex, CleanupChronology
from .browser_native_receipt import NativeZoomReceipt


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class CommandNode(_StrictModel):
    node_id: str
    outcome: str


class CommandReceipt(_StrictModel):
    command: tuple[str, ...]
    process_id: int
    started_at_ns: int
    finished_at_ns: int
    exit_code: int
    selected_node_count: int
    passed_node_count: int
    failed_node_count: int
    skipped_node_count: int
    node_manifest: tuple[CommandNode, ...]


class RunReceiptObservation(_StrictModel):
    leaf: str
    capture_names: tuple[str, ...]
    capture_count: int
    capture_files_current: bool
    cleanup: CleanupChronology
    native: NativeZoomReceipt
    command: CommandReceipt


class DeterminismReceipt(_StrictModel):
    first: RunReceiptObservation
    second: RunReceiptObservation
    capture_order_count_match: bool
    native_deterministic_projection_match: bool
    ordinary_cleanup_zero: bool
    native_cleanup_zero: bool
    command_exit_zero: bool
    node_manifest_match: bool
    passed: bool


@final
class SameRunError(Exception):
    @override
    def __str__(self) -> str:
        return "determinism requires two distinct run leaves"


@final
class DifferentGateError(Exception):
    @override
    def __str__(self) -> str:
        return "run leaves do not share one gate-owned runs directory"


@final
class OutsideGateError(Exception):
    @override
    def __str__(self) -> str:
        return "run leaves are outside the Todo 4 evidence gate"


def _validated_pair(first: Path, second: Path) -> tuple[Path, Path, Path]:
    first_resolved = first.resolve(strict=True)
    second_resolved = second.resolve(strict=True)
    if first_resolved == second_resolved:
        raise SameRunError
    if first_resolved.parent != second_resolved.parent:
        raise DifferentGateError
    runs = first_resolved.parent
    root = runs.parent
    if runs.name != "runs" or root.name != "task-4-nvidia-build-lb":
        raise OutsideGateError
    return first_resolved, second_resolved, root


def _capture_files_are_current(run: Path, index: CaptureIndex) -> bool:
    expected = {f"{capture.name}.png" for capture in index.captures}
    captures = run / "captures"
    try:
        observed = {path.name for path in captures.iterdir() if path.is_file()}
        if observed != expected or any(
            capture.native_zoom and capture.content_row_coverage < 0.5 for capture in index.captures
        ):
            return False
        for capture in index.captures:
            path = Path(capture.path).resolve(strict=True)
            expected_path = (captures / f"{capture.name}.png").resolve(strict=True)
            content = path.read_bytes()
            if path != expected_path:
                return False
            if len(content) != capture.byte_count:
                return False
            if sha256(content).hexdigest() != capture.sha256:
                return False
    except OSError:
        return False
    return True


def _load_run(run: Path) -> RunReceiptObservation:
    index = CaptureIndex.model_validate_json(
        (run / "capture-index.json").read_text(encoding="utf-8")
    )
    cleanup = CleanupChronology.model_validate_json(
        (run / "cleanup.json").read_text(encoding="utf-8")
    )
    native = NativeZoomReceipt.model_validate_json(
        (run / "native-zoom.json").read_text(encoding="utf-8")
    )
    command = CommandReceipt.model_validate_json(
        (run / "command-receipt.json").read_text(encoding="utf-8")
    )
    return RunReceiptObservation(
        leaf=run.name,
        capture_names=tuple(capture.name for capture in index.captures),
        capture_count=len(index.captures),
        capture_files_current=_capture_files_are_current(run, index),
        cleanup=cleanup,
        native=native,
        command=command,
    )


def _ordinary_cleanup_is_zero(run: RunReceiptObservation) -> bool:
    cleanup = run.cleanup.ordinary_after_cleanup
    return not any(
        (
            cleanup.fake_server_threads,
            cleanup.port_2456_listeners,
            cleanup.browser_processes,
            cleanup.playwright_drivers,
            cleanup.browser_contexts,
            cleanup.browser_pages,
            cleanup.persistent_profiles,
            cleanup.temporary_directories,
            cleanup.clipboard_nonempty,
            cleanup.capture_blackout_active,
            cleanup.axe_network_requests,
        )
    )


def _native_cleanup_is_zero(run: RunReceiptObservation) -> bool:
    native = run.native.cleanup_after
    return not any(
        (
            native.fake_server_threads,
            native.port_2456_listeners,
            native.browser_processes,
            native.playwright_drivers,
            native.browser_contexts,
            native.browser_pages,
            native.persistent_profiles,
            native.temporary_directories,
            native.clipboard_nonempty,
            native.capture_blackout_active,
            native.axe_network_requests,
        )
    )


def observe_run_receipts(first: Path, second: Path) -> DeterminismReceipt:
    first_run, second_run, _ = _validated_pair(first, second)
    first_observation = _load_run(first_run)
    second_observation = _load_run(second_run)
    capture_match = (
        first_observation.capture_count == second_observation.capture_count
        and first_observation.capture_names == second_observation.capture_names
        and first_observation.capture_files_current
        and second_observation.capture_files_current
    )
    native_match = first_observation.native.deterministic == second_observation.native.deterministic
    ordinary_zero = _ordinary_cleanup_is_zero(first_observation) and _ordinary_cleanup_is_zero(
        second_observation
    )
    native_zero = _native_cleanup_is_zero(first_observation) and _native_cleanup_is_zero(
        second_observation
    )
    command_zero = all(
        run.command.exit_code == 0
        and run.command.passed_node_count == run.command.selected_node_count
        and run.command.failed_node_count == run.command.skipped_node_count == 0
        for run in (first_observation, second_observation)
    )
    node_match = first_observation.command.node_manifest == second_observation.command.node_manifest
    passed = (
        capture_match
        and native_match
        and ordinary_zero
        and native_zero
        and command_zero
        and node_match
    )
    return DeterminismReceipt(
        first=first_observation,
        second=second_observation,
        capture_order_count_match=capture_match,
        native_deterministic_projection_match=native_match,
        ordinary_cleanup_zero=ordinary_zero,
        native_cleanup_zero=native_zero,
        command_exit_zero=command_zero,
        node_manifest_match=node_match,
        passed=passed,
    )


def compare_run_receipts(first: Path, second: Path) -> bool:
    return observe_run_receipts(first, second).passed


def _markdown(receipt: DeterminismReceipt) -> str:
    status = "PASS" if receipt.passed else "FAIL"
    capture_names = "\n".join(f"- `{name}`" for name in receipt.first.capture_names)
    return (
        "# Todo 4 deterministic observer\n\n"
        f"Status: **{status}**\n\n"
        f"- First run: `{receipt.first.leaf}`\n"
        f"- Second run: `{receipt.second.leaf}`\n"
        f"- Capture order/count match: `{receipt.capture_order_count_match}` "
        f"({receipt.first.capture_count} / {receipt.second.capture_count})\n"
        "- Native deterministic projection match: "
        f"`{receipt.native_deterministic_projection_match}`\n"
        f"- Ordinary cleanup zero: `{receipt.ordinary_cleanup_zero}`\n"
        f"- Native cleanup zero: `{receipt.native_cleanup_zero}`\n\n"
        f"- Command exit and counts clean: `{receipt.command_exit_zero}`\n"
        f"- Node manifest match: `{receipt.node_manifest_match}`\n\n"
        "## Capture order\n\n"
        f"{capture_names}\n"
    )


def write_run_comparison(first: Path, second: Path) -> DeterminismReceipt:
    _, _, root = _validated_pair(first, second)
    receipt = observe_run_receipts(first, second)
    _ = (root / "determinism.json").write_text(
        receipt.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    _ = (root / "determinism.md").write_text(_markdown(receipt), encoding="utf-8")
    return receipt


def main(arguments: Sequence[str] | None = None) -> int:
    values = tuple(argv[1:] if arguments is None else arguments)
    if len(values) != 2:
        return 64
    receipt = write_run_comparison(Path(values[0]), Path(values[1]))
    return 0 if receipt.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
