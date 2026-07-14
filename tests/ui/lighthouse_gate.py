import argparse
import statistics
import subprocess
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar, Literal, final

from pydantic import BaseModel, ConfigDict, Field

from .browser_runtime import MANAGED_BROWSERS

CHROME = MANAGED_BROWSERS / "chromium-1228" / "chrome-linux64" / "chrome"
_LIGHTHOUSE = Path(environ.get("NBLB_LIGHTHOUSE_PATH", "node_modules/.bin/lighthouse"))
_CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
_ROUTES: tuple[Literal["admin"], Literal["showcase"]] = ("admin", "showcase")
_FORM_FACTORS: tuple[Literal["mobile"], Literal["desktop"]] = ("mobile", "desktop")


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class LighthouseAudit(_StrictModel):
    route: Literal["admin", "showcase"]
    form_factor: Literal["mobile", "desktop"]
    run_scores: tuple[dict[str, int], dict[str, int], dict[str, int]]
    median_scores: dict[str, int]


class LighthouseReceipt(_StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["PASS"] = "PASS"
    image_digest: str
    chromium_revision: Literal[1228]
    chrome_path: str
    chrome_flags: tuple[Literal["--headless=new"], Literal["--no-sandbox"]]
    browser_scope: Literal["loopback public UI only"]
    lighthouse_version: Literal["13.4.0"]
    audits: tuple[LighthouseAudit, ...]
    raw_reports_retained: Literal[False]
    cold_profiles_per_audit: Literal[3]


class _RawCategory(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    score: float


class _RawReport(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    lighthouse_version: str = Field(alias="lighthouseVersion")
    categories: dict[str, _RawCategory]


@final
class _Arguments(argparse.Namespace):
    def __init__(self) -> None:
        super().__init__()
        self.evidence_dir = Path()
        self.image_digest = ""


def _arguments() -> _Arguments:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--evidence-dir", type=Path, required=True)
    _ = parser.add_argument("--image-digest", required=True)
    arguments = _Arguments()
    _ = parser.parse_args(namespace=arguments)
    return arguments


def _score_projection(report: _RawReport) -> tuple[str, dict[str, int]]:
    projected: dict[str, int] = {}
    for category in _CATEGORIES:
        row = report.categories.get(category)
        if row is None:
            reason = "Lighthouse category was missing"
            raise ValueError(reason)
        projected[category] = round(row.score * 100)
    return report.lighthouse_version, projected


def _run_once(
    route: Literal["admin", "showcase"],
    form_factor: Literal["mobile", "desktop"],
    output: Path,
) -> tuple[str, dict[str, int]]:
    command = [
        str(_LIGHTHOUSE),
        f"http://127.0.0.1:2456/{route}",
        "--quiet",
        "--output=json",
        f"--output-path={output}",
        f"--chrome-path={CHROME}",
        "--chrome-flags=--headless=new --no-sandbox",
        "--only-categories=performance,accessibility,best-practices,seo",
        "--max-wait-for-load=45000",
    ]
    if form_factor == "desktop":
        command.append("--preset=desktop")
    completed = subprocess.run(  # noqa: S603 - executable and arguments are fixed gate inputs.
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )
    if completed.returncode != 0:
        failure_class = (
            "chrome_start_failed"
            if "Unable to connect to Chrome" in completed.stderr
            else "audit_failed"
        )
        reason = f"Lighthouse execution failed: {failure_class}"
        raise AssertionError(reason)
    report = _RawReport.model_validate_json(output.read_bytes())
    return _score_projection(report)


def main() -> int:
    args = _arguments()
    if not CHROME.is_file() or not _LIGHTHOUSE.is_file():
        reason = "pinned Lighthouse or Chromium runtime is unavailable"
        raise AssertionError(reason)
    audits: list[LighthouseAudit] = []
    observed_version: str | None = None
    with TemporaryDirectory(prefix="nblb-lighthouse-") as temporary_name:
        temporary = Path(temporary_name)
        for route in _ROUTES:
            for form_factor in _FORM_FACTORS:
                scores: list[dict[str, int]] = []
                for index in range(3):
                    version, projection = _run_once(
                        route, form_factor, temporary / f"{route}-{form_factor}-{index}.json"
                    )
                    if observed_version is not None and observed_version != version:
                        reason = "Lighthouse version changed within one gate"
                        raise AssertionError(reason)
                    observed_version = version
                    scores.append(projection)
                medians = {
                    category: int(statistics.median(item[category] for item in scores))
                    for category in _CATEGORIES
                }
                if any(score != 100 for score in medians.values()):
                    reason = "Lighthouse median category score was below 100"
                    raise AssertionError(reason)
                audits.append(
                    LighthouseAudit(
                        route=route,
                        form_factor=form_factor,
                        run_scores=(scores[0], scores[1], scores[2]),
                        median_scores=medians,
                    )
                )
    assert observed_version == "13.4.0"
    receipt = LighthouseReceipt(
        image_digest=args.image_digest,
        chromium_revision=1228,
        chrome_path=str(CHROME),
        chrome_flags=("--headless=new", "--no-sandbox"),
        browser_scope="loopback public UI only",
        lighthouse_version="13.4.0",
        audits=tuple(audits),
        raw_reports_retained=False,
        cold_profiles_per_audit=3,
    )
    _ = (args.evidence_dir / "lighthouse.json").write_text(
        receipt.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
