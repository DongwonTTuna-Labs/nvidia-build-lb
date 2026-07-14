from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

    from .browser_auth import AuthenticatedSession
    from .browser_evidence import EvidenceRecorder
    from .browser_prod_client import BrowserProdAdminClient
    from .browser_prod_models import BrowserNetworkProjection


class ProductionNetworkObserver(Protocol):
    def set_phase(self, phase: str) -> None: ...

    def attach(self, page: Page) -> None: ...

    def detach(self, page: Page) -> None: ...

    def verified(self) -> tuple[BrowserNetworkProjection, ...]: ...


@dataclass(frozen=True, slots=True)
class ProductionQaContext:
    recorder: EvidenceRecorder
    client: BrowserProdAdminClient
    network: ProductionNetworkObserver
    axe_asset: Path
    run_name: Literal["run-a", "run-b"]


@dataclass(frozen=True, slots=True)
class ProductionJourney:
    qa: ProductionQaContext
    session: AuthenticatedSession
    capture_prefix: str = ""
    native_zoom: bool = False
