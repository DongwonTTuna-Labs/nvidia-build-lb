"""Permanently disabled provider logger objects and registry access."""

import logging
from typing import ClassVar, Protocol, override, runtime_checkable

from nvidia_build_lb.runtime_manifest_data import EXPECTED_LOGGER_NAMES
from nvidia_build_lb.runtime_types import PinnedRuntimeDriftError, SealedLoggerMutationError

_canonical_loggers: dict[str, "FrozenNoOpLogger"] | None = None


@runtime_checkable
class LoggerRegistry(Protocol):
    """Object-valued access to the stdlib logger registry."""

    def get(self, key: str, default: object = None) -> object:
        """Return one registered object."""
        ...

    def __setitem__(self, key: str, value: object) -> None:
        """Install one object by exact logger name."""
        ...


class FrozenNoOpLogger:
    """A permanently disabled logger installed before provider imports."""

    __slots__: ClassVar[tuple[str, ...]] = ("name",)
    name: str

    def __init__(self, name: str) -> None:
        """Set the immutable logger name exactly once."""
        self.name = name

    @property
    def disabled(self) -> bool:
        """Remain disabled independently of global logging state."""
        return True

    @property
    def handlers(self) -> tuple[()]:
        """Expose an immutable empty handler collection."""
        return ()

    @property
    def propagate(self) -> bool:
        """Never propagate provider events."""
        return False

    def isEnabledFor(self, _level: int) -> bool:  # noqa: N802
        """Disable every level."""
        return False

    def debug(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider debug events."""

    def info(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider info events."""

    def warning(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider warning events."""

    def error(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider error events."""

    def exception(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider exception events."""

    def critical(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider critical events."""

    def log(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider generic events."""

    def handle(self, *_args: object, **_kwargs: object) -> None:
        """Discard provider records."""

    def callHandlers(self, *_args: object, **_kwargs: object) -> None:  # noqa: N802
        """Never dispatch provider records."""

    def emit(self, *_args: object, **_kwargs: object) -> None:
        """Never emit provider records."""

    def format(self, *_args: object, **_kwargs: object) -> str:
        """Never render provider-controlled values."""
        return ""

    def filter(self, *_args: object, **_kwargs: object) -> bool:
        """Reject every provider record."""
        return False

    def makeRecord(self, *_args: object, **_kwargs: object) -> None:  # noqa: N802
        """Never construct a provider record."""

    def setLevel(self, _level: object) -> None:  # noqa: N802
        """Reject logger mutation."""
        raise SealedLoggerMutationError

    def addHandler(self, _handler: object) -> None:  # noqa: N802
        """Reject logger mutation."""
        raise SealedLoggerMutationError

    def removeHandler(self, _handler: object) -> None:  # noqa: N802
        """Reject logger mutation."""
        raise SealedLoggerMutationError

    def addFilter(self, _filter: object) -> None:  # noqa: N802
        """Reject logger mutation."""
        raise SealedLoggerMutationError

    def removeFilter(self, _filter: object) -> None:  # noqa: N802
        """Reject logger mutation."""
        raise SealedLoggerMutationError

    @override
    def __setattr__(self, name: str, value: object) -> None:
        """Reject every post-initialization attribute write."""
        if name == "name" and not hasattr(self, "name") and isinstance(value, str):
            object.__setattr__(self, name, value)
            return
        raise SealedLoggerMutationError

    @override
    def __delattr__(self, _name: str) -> None:
        """Reject every attribute deletion."""
        raise SealedLoggerMutationError


def install_sealed_loggers() -> dict[str, FrozenNoOpLogger]:
    """Install exact immutable objects before provider imports."""
    global _canonical_loggers  # noqa: PLW0603 - one process-lifetime identity seal.
    if _canonical_loggers is not None:
        return sealed_loggers()
    registry = logger_registry()
    installed: dict[str, FrozenNoOpLogger] = {}
    for name in sorted(EXPECTED_LOGGER_NAMES):
        logger = FrozenNoOpLogger(name)
        registry[name] = logger
        installed[name] = logger
    _canonical_loggers = installed
    return dict(installed)


def sealed_loggers() -> dict[str, FrozenNoOpLogger]:
    """Return the original sealed objects only while registry identity is intact."""
    canonical = _canonical_loggers
    if canonical is None:
        raise PinnedRuntimeDriftError
    registry = logger_registry()
    result: dict[str, FrozenNoOpLogger] = {}
    for name in EXPECTED_LOGGER_NAMES:
        logger = registry.get(name)
        expected = canonical.get(name)
        if type(expected) is not FrozenNoOpLogger or logger is not expected:
            raise PinnedRuntimeDriftError
        result[name] = expected
    return result


def provider_logger(name: str) -> FrozenNoOpLogger:
    """Return one approved sealed provider logger."""
    logger = sealed_loggers().get(name)
    if logger is None:
        raise PinnedRuntimeDriftError
    return logger


def logger_registry() -> LoggerRegistry:
    """Narrow the dynamically typed stdlib logger registry."""
    candidate = _object_boundary(logging.Logger.manager.loggerDict)
    if not isinstance(candidate, LoggerRegistry):
        raise PinnedRuntimeDriftError
    return candidate


def _object_boundary(value: object) -> object:
    return value
