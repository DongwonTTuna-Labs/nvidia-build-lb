from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import final

from playwright.sync_api import Page
from pydantic import BaseModel

from .browser_checks import evaluate_string, execute_script
from .browser_evidence_models import (
    AdversarialProbe,
    AdversarialReceipt,
    BrowserLogCount,
    BrowserObservabilityReceipt,
    BrowserProvenance,
    CaptureBlockedError,
    CaptureIndex,
    CaptureRecord,
    CaptureSpec,
    CaptureVerificationError,
    CleanupChronology,
    CleanupReceipt,
    ManualQaReceipt,
    ManualScenario,
    ResourceObservation,
    ScrollPosition,
)
from .browser_png import capture_native_document_png, decode_rgb_png, informative_row_coverage
from .evidence_paths import reserve_capture_directory

__all__ = (
    "AdversarialProbe",
    "AdversarialReceipt",
    "BrowserLogCount",
    "BrowserObservabilityReceipt",
    "BrowserProvenance",
    "CaptureBlockedError",
    "CaptureIndex",
    "CaptureRecord",
    "CaptureSpec",
    "CaptureVerificationError",
    "CleanupChronology",
    "CleanupReceipt",
    "EvidenceRecorder",
    "ManualQaReceipt",
    "ManualScenario",
    "ResourceObservation",
)


@final
class EvidenceRecorder:
    __slots__ = ("_blackout", "_captures", "_directory", "_scenarios")

    def __init__(self, directory: Path, gate_root: Path | None = None) -> None:
        super().__init__()
        self._directory = directory
        self._captures: list[CaptureRecord] = []
        self._scenarios: list[ManualScenario] = []
        self._blackout: str | None = None
        _ = reserve_capture_directory(directory, gate_root)

    def begin_blackout(self, reason: str) -> None:
        self._blackout = reason

    def end_blackout(self) -> None:
        self._blackout = None

    def blackout_active(self) -> bool:
        return self._blackout is not None

    def add_scenario(self, scenario: ManualScenario) -> None:
        self._scenarios.append(scenario)

    def scenarios(self) -> tuple[ManualScenario, ...]:
        return tuple(self._scenarios)

    def captures(self) -> tuple[CaptureRecord, ...]:
        return tuple(self._captures)

    def add_capture_records(self, records: Sequence[CaptureRecord]) -> None:
        self._captures.extend(records)

    def verified_capture_index(self, expected_names: Sequence[str]) -> CaptureIndex:
        captures = self.captures()
        names = tuple(record.name for record in captures)
        files = {path.name for path in (self._directory / "captures").iterdir() if path.is_file()}
        if names != tuple(expected_names):
            reason = "capture name order or count does not match the immutable manifest"
            raise CaptureVerificationError(reason)
        if files != {f"{name}.png" for name in tuple(expected_names)}:
            reason = "capture directory file set does not match the required manifest"
            raise CaptureVerificationError(reason)
        for record in captures:
            content = Path(record.path).read_bytes()
            if sha256(content).hexdigest() != record.sha256 or len(content) != record.byte_count:
                reason = "capture digest or byte count does not match the stored artifact"
                raise CaptureVerificationError(reason)
        return CaptureIndex(captures=captures)

    def capture(self, page: Page, spec: CaptureSpec) -> None:
        if self._blackout is not None:
            raise CaptureBlockedError(self._blackout)
        path = (self._directory / "captures" / f"{spec.name}.png").resolve()
        scroll = ScrollPosition.model_validate_json(
            evaluate_string(
                page,
                "() => JSON.stringify({x: window.scrollX, y: window.scrollY})",
            )
        )
        execute_script(page, "() => window.scrollTo(0, 0)")
        try:
            if spec.native_zoom:
                content = capture_native_document_png(page)
                _ = path.write_bytes(content)
            else:
                content = page.screenshot(path=str(path), full_page=True)
        finally:
            execute_script(page, f"() => window.scrollTo({scroll.x}, {scroll.y})")
        source_paths = (
            "src/nvidia_build_lb/web/templates/admin.html",
            "src/nvidia_build_lb/web/templates/showcase.html",
            "src/nvidia_build_lb/web/static/admin.css",
            "src/nvidia_build_lb/web/static/showcase.css",
            "src/nvidia_build_lb/web/static/admin.js",
        )
        repository_root = Path(__file__).resolve().parents[2]
        source_newest = max(
            (repository_root / source).stat().st_mtime_ns for source in source_paths
        )
        capture_mtime = path.stat().st_mtime_ns
        if capture_mtime <= source_newest:
            reason = "capture is not newer than every rendered source"
            raise CaptureVerificationError(reason)
        image = decode_rgb_png(content)
        width_metric = "document.documentElement.scrollWidth"
        dimension_script = f"""() => JSON.stringify({{
x:{width_metric}*devicePixelRatio,
y:document.documentElement.scrollHeight*devicePixelRatio
}})"""
        dimensions = ScrollPosition.model_validate_json(evaluate_string(page, dimension_script))
        expected_width = round(dimensions.x)
        expected_height = round(dimensions.y)
        width_candidates = {expected_width, expected_width + int(spec.native_zoom)}
        height_candidates = {expected_height, expected_height + int(spec.native_zoom)}
        if image.width not in width_candidates or image.height not in height_candidates:
            reason = "capture raster does not reach the right and bottom landmarks"
            raise CaptureVerificationError(reason)
        coverage = informative_row_coverage(image)
        if (spec.reduced_motion or spec.native_zoom) and coverage < 0.5:
            reason = "required capture is not information-complete"
            raise CaptureVerificationError(reason)
        self.add_capture_records(
            (
                CaptureRecord(
                    name=spec.name,
                    route=page.url.split("?", maxsplit=1)[0],
                    state=spec.state,
                    viewport=spec.viewport,
                    reduced_motion=spec.reduced_motion,
                    native_zoom=spec.native_zoom,
                    path=str(path),
                    sha256=sha256(content).hexdigest(),
                    byte_count=len(content),
                    source_newest_mtime_ns=source_newest,
                    capture_mtime_ns=capture_mtime,
                    source_paths=source_paths,
                    pixel_width=image.width,
                    pixel_height=image.height,
                    landmarks=(f"right-edge:{image.width}", f"bottom-edge:{image.height}"),
                    content_row_coverage=coverage,
                ),
            )
        )

    def write(self, name: str, receipt: BaseModel) -> None:
        path = self._directory / name
        _ = path.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
