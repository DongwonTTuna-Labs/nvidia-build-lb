"""Lazy PostgreSQL testcontainer lifecycle with an injectable local fake."""

from collections.abc import Callable
from importlib import import_module
from types import TracebackType
from typing import ClassVar, Protocol, Self, runtime_checkable

from pydantic import SecretStr


class PostgresContainerLike(Protocol):
    """Small structural boundary used by the lazy harness."""

    def start(self) -> Self:
        """Start the container and return itself."""
        ...

    def stop(self) -> None:
        """Stop and remove the container."""
        ...

    def get_connection_url(self) -> str:
        """Return the driver connection URL."""
        ...


type ContainerFactory = Callable[[str], PostgresContainerLike]


class PostgresHarnessNotStartedError(RuntimeError):
    """The caller requested a DSN before explicitly starting the harness."""


class _PostgresConstructor(Protocol):
    def __call__(self, image: str) -> PostgresContainerLike:
        """Construct an unstarted PostgreSQL testcontainer."""
        ...


@runtime_checkable
class _PostgresModule(Protocol):
    PostgresContainer: _PostgresConstructor


def _default_factory(image: str) -> PostgresContainerLike:
    module = import_module("testcontainers.postgres")
    if not isinstance(module, _PostgresModule):
        raise TypeError
    return module.PostgresContainer(image)


class LazyPostgresHarness:
    """Delay testcontainers import and Docker access until explicit start."""

    __slots__: ClassVar[tuple[str, ...]] = ("_container", "factory", "image")

    image: str
    factory: ContainerFactory
    _container: PostgresContainerLike | None

    def __init__(
        self,
        image: str = "postgres:17-alpine",
        factory: ContainerFactory = _default_factory,
    ) -> None:
        self.image = image
        self.factory = factory
        self._container = None

    @property
    def started(self) -> bool:
        """Report whether this harness owns a started container."""
        return self._container is not None

    def start(self) -> None:
        """Start exactly one container on first explicit use."""
        if self._container is not None:
            return
        candidate = self.factory(self.image)
        _ = candidate.start()
        self._container = candidate

    def connection_url(self) -> SecretStr:
        """Return a masked DSN only after successful start."""
        if self._container is None:
            raise PostgresHarnessNotStartedError
        return SecretStr(self._container.get_connection_url())

    def stop(self) -> None:
        """Stop a started container and make repeated cleanup a no-op."""
        container = self._container
        self._container = None
        if container is not None:
            container.stop()

    def __enter__(self) -> Self:
        """Start the lazy resource for an explicit context."""
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always release an explicitly entered resource."""
        self.stop()
