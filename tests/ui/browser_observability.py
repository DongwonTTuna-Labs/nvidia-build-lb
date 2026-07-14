from typing import ClassVar, Protocol, final

from playwright.sync_api import CDPSession, ConsoleMessage, Error, Page, Request, Response
from pydantic import BaseModel, ConfigDict

type _CdpValue = str | int | float | bool | None | list["_CdpValue"] | dict[str, "_CdpValue"]


class _StrictModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid", frozen=True)


class _CdpProjection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)


class _RuntimeConsolePayload(_CdpProjection):
    type: str


class _BrowserLogEntry(_CdpProjection):
    source: str
    level: str


class _BrowserLogPayload(_CdpProjection):
    entry: _BrowserLogEntry


class PhaseKind(_StrictModel):
    phase: str
    kind: str


class BrowserLogObservation(_StrictModel):
    phase: str
    source: str
    level: str


class RequestFailureObservation(_StrictModel):
    phase: str
    method: str


class _ConsoleKind(Protocol):
    @property
    def type(self) -> str: ...


class _RequestMethod(Protocol):
    @property
    def method(self) -> str: ...


class _ResponseRequest(Protocol):
    @property
    def request(self) -> _RequestMethod: ...


class _CdpSender(Protocol):
    def send(self, method: str) -> dict[str, _CdpValue]: ...


def _enable_cdp_domain(session: _CdpSender, domain: str) -> None:
    _ = session.send(f"{domain}.enable")


@final
class PageAudit:
    __slots__ = (
        "_cdp",
        "_request_failures_with_requests",
        "_response_requests",
        "browser_logs",
        "page_console_calls",
        "page_errors",
        "phase",
        "request_count",
        "request_failures",
        "response_count",
        "runtime_console_calls",
        "runtime_exceptions",
    )

    def __init__(self) -> None:
        self._cdp: CDPSession | None = None
        self._request_failures_with_requests: list[
            tuple[_RequestMethod, RequestFailureObservation]
        ] = []
        self._response_requests: list[_RequestMethod] = []
        self.browser_logs: list[BrowserLogObservation] = []
        self.page_console_calls: list[PhaseKind] = []
        self.page_errors: list[str] = []
        self.phase = "unclassified"
        self.request_count = 0
        self.request_failures: list[RequestFailureObservation] = []
        self.response_count = 0
        self.runtime_console_calls: list[PhaseKind] = []
        self.runtime_exceptions: list[str] = []

    @property
    def console_errors(self) -> int:
        return sum(item.kind == "error" for item in self.page_console_calls)

    def set_phase(self, phase: str) -> None:
        self.phase = phase

    def unanswered_request_failures(self) -> tuple[RequestFailureObservation, ...]:
        return tuple(
            observation
            for request, observation in self._request_failures_with_requests
            if not any(request is response_request for response_request in self._response_requests)
        )

    def attach(self, page: Page) -> None:
        def on_console(message: ConsoleMessage) -> None:
            self.observe_console(message)

        def on_page_error(error: Error) -> None:
            self.observe_page_error(error)

        def on_response(response: Response) -> None:
            self.observe_response(response)

        def on_request(request: Request) -> None:
            self.observe_request(request)

        def on_request_failed(request: Request) -> None:
            self.observe_request_failed(request)

        def on_runtime_console(payload: _CdpValue) -> None:
            self.observe_runtime_console(payload)

        def on_runtime_exception(payload: _CdpValue) -> None:
            self.observe_runtime_exception(payload)

        def on_browser_log(payload: _CdpValue) -> None:
            self.observe_browser_log(payload)

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        cdp = page.context.new_cdp_session(page)
        cdp.on("Runtime.consoleAPICalled", on_runtime_console)
        cdp.on("Runtime.exceptionThrown", on_runtime_exception)
        cdp.on("Log.entryAdded", on_browser_log)
        _enable_cdp_domain(cdp, "Runtime")
        _enable_cdp_domain(cdp, "Log")
        self._cdp = cdp

    def detach(self) -> None:
        if self._cdp is not None:
            self._cdp.detach()
            self._cdp = None

    def observe_console(self, message: _ConsoleKind) -> None:
        self.page_console_calls.append(PhaseKind(phase=self.phase, kind=message.type))

    def observe_page_error(self, error: Error) -> None:
        del error
        self.page_errors.append(self.phase)

    def observe_response(self, response: _ResponseRequest) -> None:
        self._response_requests.append(response.request)
        self.response_count += 1

    def observe_request(self, request: _RequestMethod) -> None:
        del request
        self.request_count += 1

    def observe_request_failed(self, request: _RequestMethod) -> None:
        observation = RequestFailureObservation(
            phase=self.phase,
            method=request.method,
        )
        self.request_failures.append(observation)
        self._request_failures_with_requests.append((request, observation))

    def observe_runtime_console(self, payload: _CdpValue) -> None:
        event = _RuntimeConsolePayload.model_validate(payload)
        self.runtime_console_calls.append(PhaseKind(phase=self.phase, kind=event.type))

    def observe_runtime_exception(self, payload: _CdpValue) -> None:
        del payload
        self.runtime_exceptions.append(self.phase)

    def observe_browser_log(self, payload: _CdpValue) -> None:
        event = _BrowserLogPayload.model_validate(payload)
        self.browser_logs.append(
            BrowserLogObservation(
                phase=self.phase,
                source=event.entry.source,
                level=event.entry.level,
            )
        )
