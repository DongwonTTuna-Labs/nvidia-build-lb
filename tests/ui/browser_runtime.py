from dataclasses import dataclass
from os import environ
from pathlib import Path
from socket import AF_INET, SO_REUSEADDR, SOCK_STREAM, SOL_SOCKET, socket
from tempfile import gettempdir
from threading import Thread
from typing import final, override

from playwright.sync_api import Browser, Playwright, sync_playwright
from uvicorn import Config, Server

from .browser_fonts import BrowserFontEnvironment, create_browser_font_environment
from .fake_admin_server import create_fake_admin_app
from .fake_admin_state import FakeAdminState

UI_AUTHORITY = "127.0.0.1:2456"
UI_ORIGIN = f"http://{UI_AUTHORITY}"


def resolve_managed_browsers(configured: str | None, home: Path) -> Path:
    """Resolve the one Playwright cache root without accepting a relative path."""
    root = Path(configured) if configured is not None else home / ".cache" / "ms-playwright"
    if not root.is_absolute():
        reason = "PLAYWRIGHT_BROWSERS_PATH must be absolute"
        raise ValueError(reason)
    return root.resolve(strict=False)


MANAGED_BROWSERS = resolve_managed_browsers(environ.get("PLAYWRIGHT_BROWSERS_PATH"), Path.home())
_PROCESS_ROOT = Path("/proc")
_TEMP_ROOT = Path(gettempdir())
_TEMP_PREFIXES = (
    "nblb-fontconfig-",
    "nblb-native-zoom-",
    "playwright-artifacts-",
    "playwright_chromiumdev_profile-",
)


@dataclass(frozen=True, slots=True)
@final
class PortUnavailableError(Exception):
    authority: str

    @override
    def __str__(self) -> str:
        return f"task browser authority unavailable: {self.authority}"


@dataclass(frozen=True, slots=True)
@final
class BrowserRuntimeError(Exception):
    reason: str

    @override
    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class RunningFakeServer:
    state: FakeAdminState
    listener: socket
    server: Server
    thread: Thread


@dataclass(frozen=True, slots=True)
class ManagedBrowserSession:
    playwright: Playwright
    browser: Browser
    executable_path: Path
    version: str
    process_ids: tuple[int, ...]
    process_executables: tuple[Path, ...]
    temporary_paths: tuple[Path, ...]
    driver_ids: tuple[int, ...]
    baseline_browser_processes: int
    baseline_playwright_drivers: int
    baseline_temporary_paths: int
    font_environment: BrowserFontEnvironment


@dataclass(frozen=True, slots=True)
class BrowserResourceSnapshot:
    processes: tuple[tuple[int, Path], ...]
    temporary_paths: tuple[Path, ...]
    drivers: tuple[int, ...] = ()


def _approved_browser_roots() -> tuple[Path, Path]:
    return (
        (MANAGED_BROWSERS / "chromium-1228").resolve(strict=False),
        (MANAGED_BROWSERS / "chromium_headless_shell-1228").resolve(strict=False),
    )


def browser_resource_snapshot() -> BrowserResourceSnapshot:
    processes: list[tuple[int, Path]] = []
    drivers: list[int] = []
    for candidate in _PROCESS_ROOT.iterdir():
        if not candidate.name.isdecimal():
            continue
        try:
            executable = (candidate / "exe").resolve(strict=True)
        except OSError:
            continue
        if any(executable.is_relative_to(root) for root in _approved_browser_roots()):
            processes.append((int(candidate.name), executable))
        try:
            command = (candidate / "cmdline").read_bytes()
        except OSError:
            continue
        if b"playwright/driver/package/cli.js\x00run-driver" in command:
            drivers.append(int(candidate.name))
    temporary_paths = tuple(
        sorted(
            path
            for path in _TEMP_ROOT.iterdir()
            if path.is_dir() and path.name.startswith(_TEMP_PREFIXES)
        )
    )
    return BrowserResourceSnapshot(
        processes=tuple(sorted(processes)),
        temporary_paths=temporary_paths,
        drivers=tuple(sorted(drivers)),
    )


def managed_process_count(runtime: ManagedBrowserSession) -> int:
    live_processes = {process_id for process_id, _ in browser_resource_snapshot().processes}
    return len(live_processes.intersection(runtime.process_ids))


def managed_temporary_path_count(runtime: ManagedBrowserSession) -> int:
    return sum(path.exists() for path in runtime.temporary_paths)


def managed_driver_count(runtime: ManagedBrowserSession) -> int:
    return len(set(browser_resource_snapshot().drivers).intersection(runtime.driver_ids))


def new_managed_processes(
    baseline: BrowserResourceSnapshot,
    observed: BrowserResourceSnapshot,
) -> tuple[tuple[int, Path], ...]:
    baseline_processes = {process_id for process_id, _ in baseline.processes}
    return tuple(item for item in observed.processes if item[0] not in baseline_processes)


def _run_server(server: Server, listener: socket) -> None:
    server.run(sockets=[listener])


def start_fake_server() -> RunningFakeServer:
    listener = socket(AF_INET, SOCK_STREAM)
    listener.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", 2456))
    except OSError as error:
        listener.close()
        raise PortUnavailableError(UI_AUTHORITY) from error
    listener.listen(2048)
    state = FakeAdminState()
    server = Server(
        Config(
            create_fake_admin_app(state),
            host="127.0.0.1",
            port=2456,
            log_level="critical",
            log_config=None,
            access_log=False,
            lifespan="off",
        )
    )
    thread = Thread(
        target=_run_server,
        args=(server, listener),
        name="nblb-ui-fake",
        daemon=False,
    )
    thread.start()
    return RunningFakeServer(state=state, listener=listener, server=server, thread=thread)


def stop_fake_server(runtime: RunningFakeServer) -> None:
    runtime.server.should_exit = True
    runtime.thread.join(timeout=10)
    runtime.listener.close()
    if runtime.thread.is_alive():
        reason = "task fake server did not stop"
        raise BrowserRuntimeError(reason)


def authority_is_free() -> bool:
    probe = socket(AF_INET, SOCK_STREAM)
    probe.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", 2456))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def start_managed_browser() -> ManagedBrowserSession:
    chromium_dir, headless_dir = _approved_browser_roots()
    if not chromium_dir.is_dir() or not headless_dir.is_dir():
        reason = "Playwright-managed Chromium revision 1228 is unavailable"
        raise BrowserRuntimeError(reason)
    baseline = browser_resource_snapshot()
    font_environment = create_browser_font_environment()
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=True, env=font_environment.variables)
    try:
        executable = Path(playwright.chromium.executable_path).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        browser.close()
        playwright.stop()
        reason = "Playwright selected an unavailable browser executable"
        raise BrowserRuntimeError(reason) from error
    approved_roots = (chromium_dir, headless_dir)
    if not any(executable.is_relative_to(root) for root in approved_roots):
        browser.close()
        playwright.stop()
        reason = "Playwright selected a browser outside revision 1228 cache"
        raise BrowserRuntimeError(reason)
    observed = browser_resource_snapshot()
    processes = new_managed_processes(baseline, observed)
    baseline_paths = set(baseline.temporary_paths)
    temporary_paths = tuple(path for path in observed.temporary_paths if path not in baseline_paths)
    if not processes or not all(
        any(path.is_relative_to(root) for root in approved_roots) for _, path in processes
    ):
        browser.close()
        playwright.stop()
        reason = "Playwright browser process provenance is not revision 1228"
        raise BrowserRuntimeError(reason)
    return ManagedBrowserSession(
        playwright=playwright,
        browser=browser,
        executable_path=executable,
        version=browser.version,
        process_ids=tuple(process_id for process_id, _ in processes),
        process_executables=tuple(sorted({path for _, path in processes})),
        temporary_paths=temporary_paths,
        driver_ids=tuple(sorted(set(observed.drivers) - set(baseline.drivers))),
        baseline_browser_processes=len(baseline.processes),
        baseline_playwright_drivers=len(baseline.drivers),
        baseline_temporary_paths=len(baseline.temporary_paths),
        font_environment=font_environment,
    )


def stop_managed_browser(runtime: ManagedBrowserSession) -> None:
    try:
        runtime.browser.close()
    finally:
        try:
            runtime.playwright.stop()
        finally:
            runtime.font_environment.temporary.cleanup()
