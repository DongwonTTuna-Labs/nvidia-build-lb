"""Narrow structural types and construction for the isolated H1 core."""

import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from nvidia_build_lb.pinned_httpx import httpx2
from nvidia_build_lb.pinned_runtime import load_isolated_runtime
from nvidia_build_lb.transport_common import PinnedTransportDriftError


class CoreURL(Protocol):
    """Structural isolated URL fields."""

    scheme: bytes
    host: bytes
    port: int | None
    target: bytes


class CoreRequest(Protocol):
    """Structural isolated request fields."""

    method: bytes
    url: CoreURL
    headers: list[tuple[bytes, bytes]]
    extensions: dict[str, object]


class CoreAsyncStream(Protocol):
    """Single isolated response-body iterator."""

    def __aiter__(self) -> AsyncIterator[bytes]:
        """Return the raw-byte iterator."""
        ...

    async def aclose(self) -> None:
        """Retire the physical response."""
        ...


class CoreResponse(Protocol):
    """Structural isolated response fields."""

    status: int
    headers: list[tuple[bytes, bytes]]
    stream: CoreAsyncStream
    extensions: dict[str, object]


class CorePool(Protocol):
    """The exact application-lifetime isolated pool surface."""

    async def handle_async_request(self, request: CoreRequest) -> CoreResponse:
        """Perform one request without helper retry."""
        ...

    async def aclose(self) -> None:
        """Close the pool."""
        ...


class URLFactory(Protocol):
    """Construct one isolated URL."""

    def __call__(
        self,
        *,
        scheme: bytes,
        host: bytes,
        port: int | None,
        target: bytes,
    ) -> CoreURL:
        """Construct from exact raw URL fields."""
        ...


class RequestFactory(Protocol):
    """Construct one isolated request."""

    def __call__(
        self,
        method: str,
        url: CoreURL,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        content: object,
        extensions: dict[str, object],
    ) -> CoreRequest:
        """Construct from the immutable application request."""
        ...


@runtime_checkable
class ModelsModule(Protocol):
    """Narrow approved isolated model constructors."""

    URL: URLFactory
    Request: RequestFactory


class PoolFactory(Protocol):
    """Construct the fixed isolated pool."""

    def __call__(self, **kwargs: object) -> CorePool:
        """Construct with explicit fixed options."""
        ...


@runtime_checkable
class PoolModule(Protocol):
    """Narrow the approved isolated pool module."""

    AsyncConnectionPool: PoolFactory


class BackendFactory(Protocol):
    """Construct the approved AnyIO backend."""

    def __call__(self) -> object:
        """Construct the backend."""
        ...


@runtime_checkable
class BackendModule(Protocol):
    """Narrow the approved backend module."""

    AnyIOBackend: BackendFactory


@runtime_checkable
class ExceptionsModule(Protocol):
    """Closed isolated exception type surface."""

    PoolTimeout: type[Exception]
    ConnectTimeout: type[Exception]
    ReadTimeout: type[Exception]
    WriteTimeout: type[Exception]
    ConnectError: type[Exception]
    ReadError: type[Exception]
    WriteError: type[Exception]
    UnsupportedProtocol: type[Exception]
    LocalProtocolError: type[Exception]
    RemoteProtocolError: type[Exception]
    ProtocolError: type[Exception]
    TimeoutException: type[Exception]
    NetworkError: type[Exception]
    ProxyError: type[Exception]


@dataclass(frozen=True, slots=True)
class TransportConfiguration:
    """Observable fixed H1 transport configuration."""

    http1: bool = True
    http2: bool = False
    retries: int = 0
    max_connections: int = 32
    max_keepalive_connections: int = 16
    keepalive_expiry_seconds: float = 30.0
    proxy: None = None


@dataclass(frozen=True, slots=True)
class TransportCore:
    """Narrow constructors, pool, and closed exception mapping."""

    url_factory: URLFactory
    request_factory: RequestFactory
    pool: CorePool
    error_map: tuple[tuple[type[Exception], type[httpx2.RequestError], str], ...]
    proxy_error: type[Exception]


def load_transport_core(*, pool_override: CorePool | None = None) -> TransportCore:
    """Narrow verified modules and construct the fixed no-retry H1 pool."""
    runtime = load_isolated_runtime()
    models: object = runtime.models
    pool_module: object = runtime.connection_pool
    backend: object = runtime.anyio_backend
    exceptions: object = runtime.exceptions
    if not (
        isinstance(models, ModelsModule)
        and isinstance(pool_module, PoolModule)
        and isinstance(backend, BackendModule)
        and isinstance(exceptions, ExceptionsModule)
    ):
        raise PinnedTransportDriftError
    configuration = TransportConfiguration()
    pool = pool_override or pool_module.AsyncConnectionPool(
        ssl_context=ssl.create_default_context(),
        proxy=None,
        max_connections=configuration.max_connections,
        max_keepalive_connections=configuration.max_keepalive_connections,
        keepalive_expiry=configuration.keepalive_expiry_seconds,
        http1=True,
        http2=False,
        retries=0,
        network_backend=backend.AnyIOBackend(),
    )
    error_map = (
        (exceptions.PoolTimeout, httpx2.PoolTimeout, "pool_timeout"),
        (exceptions.ConnectTimeout, httpx2.ConnectTimeout, "connect_timeout"),
        (exceptions.ReadTimeout, httpx2.ReadTimeout, "read_timeout"),
        (exceptions.WriteTimeout, httpx2.WriteTimeout, "write_timeout"),
        (exceptions.ConnectError, httpx2.ConnectError, "connect_error"),
        (exceptions.ReadError, httpx2.ReadError, "read_error"),
        (exceptions.WriteError, httpx2.WriteError, "write_error"),
        (exceptions.UnsupportedProtocol, httpx2.UnsupportedProtocol, "unsupported_protocol"),
        (exceptions.LocalProtocolError, httpx2.LocalProtocolError, "local_protocol_error"),
        (exceptions.RemoteProtocolError, httpx2.RemoteProtocolError, "remote_protocol_error"),
        (exceptions.ProtocolError, httpx2.ProtocolError, "protocol_error"),
        (exceptions.TimeoutException, httpx2.TimeoutException, "timeout"),
        (exceptions.NetworkError, httpx2.NetworkError, "network_error"),
    )
    return TransportCore(models.URL, models.Request, pool, error_map, exceptions.ProxyError)


def mapped_error(
    error: Exception,
    request: httpx2.Request,
    core: TransportCore,
) -> Exception:
    """Return one closed safe exception without reading provider-controlled text."""
    if isinstance(error, core.proxy_error):
        return PinnedTransportDriftError()
    for source_type, target_type, safe_code in core.error_map:
        if isinstance(error, source_type):
            return target_type(safe_code, request=request)
    return PinnedTransportDriftError()
