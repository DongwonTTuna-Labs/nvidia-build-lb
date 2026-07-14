from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from playwright.sync_api import Browser, Page

from .browser_checks import (
    ReducedMotionObservation,
    ShowcaseDesktopObservation,
    assert_no_page_overflow,
    axe_counts,
    reduced_motion_observation,
    showcase_desktop_observation,
)
from .browser_evidence import CaptureSpec, EvidenceRecorder, ManualScenario
from .browser_observability import PageAudit
from .browser_observability_contract import assert_public_observability_clean
from .browser_runtime import UI_ORIGIN


@dataclass(frozen=True, slots=True)
class PublicQaResult:
    axe_serious: int
    axe_critical: int
    console_errors: int
    page_errors: int
    axe_network_requests: int
    request_events: int
    response_events: int
    failed_request_events: int
    reduced_motion: ReducedMotionObservation
    desktop_layout: ShowcaseDesktopObservation


@dataclass(frozen=True, slots=True)
class ViewportSpec:
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True, slots=True)
class _PublicPageResult:
    serious: int
    critical: int
    axe_network_requests: int
    audit: PageAudit
    desktop_layout: ShowcaseDesktopObservation | None = None


class _Closable(Protocol):
    def close(self) -> None: ...


class _NetworkAudit(Protocol):
    def set_phase(self, phase: str) -> None: ...

    def attach(self, page: Page) -> None: ...

    def detach(self, page: Page) -> None: ...


def close_public_page(audit: PageAudit, page: _Closable, context: _Closable) -> None:
    try:
        assert_public_observability_clean(audit)
    finally:
        try:
            audit.detach()
        finally:
            try:
                page.close()
            finally:
                context.close()


def _capture_viewport(
    browser: Browser,
    recorder: EvidenceRecorder,
    axe_asset: Path,
    viewport: ViewportSpec,
    network_audit: _NetworkAudit | None,
) -> _PublicPageResult:
    context = browser.new_context(viewport={"width": viewport.width, "height": viewport.height})
    page = context.new_page()
    page.set_default_timeout(5_000)
    audit = PageAudit()
    audit.set_phase(f"public_admin_{viewport.width}")
    audit.attach(page)
    if network_audit is not None:
        network_audit.set_phase(f"public_admin_{viewport.width}")
        network_audit.attach(page)
    desktop_layout: ShowcaseDesktopObservation | None = None
    try:
        _ = page.goto(f"{UI_ORIGIN}/admin", wait_until="domcontentloaded")
        assert_no_page_overflow(page)
        admin_counts = axe_counts(page, axe_asset)
        recorder.capture(
            page,
            CaptureSpec(
                name=f"admin-login-{viewport.width}",
                state="login",
                viewport=viewport.label,
            ),
        )
        audit.set_phase(f"public_showcase_{viewport.width}")
        if network_audit is not None:
            network_audit.set_phase(f"public_showcase_{viewport.width}")
        _ = page.goto(f"{UI_ORIGIN}/showcase", wait_until="domcontentloaded")
        assert_no_page_overflow(page)
        if viewport.width == 1280:
            desktop_layout = showcase_desktop_observation(page)
        showcase_counts = axe_counts(page, axe_asset)
        recorder.capture(
            page,
            CaptureSpec(
                name=f"showcase-{viewport.width}",
                state="default hover focus active disabled loading empty error CJK",
                viewport=viewport.label,
            ),
        )
    finally:
        if network_audit is not None:
            network_audit.detach(page)
        close_public_page(audit, page, context)
    return _PublicPageResult(
        serious=admin_counts.serious + showcase_counts.serious,
        critical=admin_counts.critical + showcase_counts.critical,
        axe_network_requests=admin_counts.network_requests + showcase_counts.network_requests,
        audit=audit,
        desktop_layout=desktop_layout,
    )


def _capture_reduced_motion(
    browser: Browser,
    recorder: EvidenceRecorder,
    network_audit: _NetworkAudit | None,
) -> tuple[ReducedMotionObservation, PageAudit]:
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        reduced_motion="reduce",
    )
    page = context.new_page()
    page.set_default_timeout(5_000)
    audit = PageAudit()
    audit.set_phase("public_showcase_reduced_motion")
    audit.attach(page)
    if network_audit is not None:
        network_audit.set_phase("public_showcase_reduced_motion")
        network_audit.attach(page)
    try:
        _ = page.goto(f"{UI_ORIGIN}/showcase", wait_until="domcontentloaded")
        assert_no_page_overflow(page)
        reduced_motion = reduced_motion_observation(page)
        recorder.capture(
            page,
            CaptureSpec(
                name="showcase-reduced-motion",
                state="reduced motion",
                viewport="1280x900",
                reduced_motion=True,
            ),
        )
    finally:
        if network_audit is not None:
            network_audit.detach(page)
        close_public_page(audit, page, context)
    return reduced_motion, audit


def capture_public_surfaces(
    browser: Browser,
    recorder: EvidenceRecorder,
    axe_asset: Path,
    network_audit: _NetworkAudit | None = None,
) -> PublicQaResult:
    pages = tuple(
        _capture_viewport(browser, recorder, axe_asset, viewport, network_audit)
        for viewport in (ViewportSpec(375, 900), ViewportSpec(768, 900), ViewportSpec(1280, 900))
    )
    reduced_motion, reduced_audit = _capture_reduced_motion(browser, recorder, network_audit)
    audits = (*tuple(result.audit for result in pages), reduced_audit)
    desktop_layout = next(
        result.desktop_layout for result in pages if result.desktop_layout is not None
    )
    recorder.add_scenario(
        ManualScenario(
            name="public responsive surfaces",
            actions=(
                "GET /admin and /showcase at 375x900, 768x900, and 1280x900",
                "Open /showcase with prefers-reduced-motion: reduce",
                "Inject local official axe-core test asset through Playwright evaluation",
            ),
            observables=(
                "No page-level horizontal overflow",
                "Fresh secret-free PNG per route and viewport",
                "Reduced-motion capture remains information-complete",
            ),
        )
    )
    return PublicQaResult(
        axe_serious=sum(result.serious for result in pages),
        axe_critical=sum(result.critical for result in pages),
        console_errors=sum(audit.console_errors for audit in audits),
        page_errors=sum(len(audit.page_errors) for audit in audits),
        axe_network_requests=sum(result.axe_network_requests for result in pages),
        request_events=sum(audit.request_count for audit in audits),
        response_events=sum(audit.response_count for audit in audits),
        failed_request_events=sum(len(audit.request_failures) for audit in audits),
        reduced_motion=reduced_motion,
        desktop_layout=desktop_layout,
    )
