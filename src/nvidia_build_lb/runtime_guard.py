"""Per-boundary runtime identity guard for the pinned provider path."""

import importlib.metadata
import sys
from pathlib import Path
from types import ModuleType
from typing import ClassVar

from nvidia_build_lb.runtime_logger import (
    FrozenNoOpLogger,
    logger_registry,
    sealed_loggers,
)
from nvidia_build_lb.runtime_manifest import forbidden_loaded_modules, verify_runtime_closure
from nvidia_build_lb.runtime_manifest_data import ISOLATED_NAMESPACE, MODULE_ORDER
from nvidia_build_lb.runtime_types import PinnedRuntimeDriftError, RuntimeGuardSnapshot

__all__ = ["TransportBoundaryGuard", "verify_runtime_closure"]

_canonical_isolated_modules: tuple[tuple[str, ModuleType, Path], ...] | None = None
_canonical_namespace_modules: tuple[tuple[str, ModuleType], ...] | None = None


class TransportBoundaryGuard:
    """Recheck logger and module identity at every transport boundary."""

    __slots__: ClassVar[tuple[str, ...]] = (
        "_isolated_modules",
        "_logger_globals",
        "_loggers",
        "_namespace_modules",
    )

    _isolated_modules: tuple[tuple[str, ModuleType, Path], ...]
    _logger_globals: tuple[tuple[ModuleType, FrozenNoOpLogger], ...]
    _loggers: dict[str, FrozenNoOpLogger]
    _namespace_modules: tuple[tuple[str, ModuleType], ...]

    def __init__(self, isolated_module_names: tuple[str, ...] = ()) -> None:
        """Bind to the process-lifetime logger and isolated-module identities."""
        self._loggers = sealed_loggers()
        if not isolated_module_names and any(is_isolated_name(name) for name in sys.modules):
            isolated_module_names = approved_module_names()
        self._isolated_modules, self._namespace_modules = _canonical_isolated_state(
            isolated_module_names
        )
        self._logger_globals = _capture_logger_globals(isolated_module_names, self._loggers)
        self.observe()

    def observe(self) -> None:
        """Fail on logger replacement, forbidden imports, or isolated drift."""
        registry = logger_registry()
        if any(registry.get(name) is not logger for name, logger in self._loggers.items()):
            raise PinnedRuntimeDriftError
        if any(
            not logger.disabled or logger.handlers or logger.propagate
            for logger in self._loggers.values()
        ):
            raise PinnedRuntimeDriftError
        if forbidden_loaded_modules():
            raise PinnedRuntimeDriftError
        expected_modules = {name for name, _module, _path in self._isolated_modules}
        expected_namespaces = {name for name, _module in self._namespace_modules}
        if {name for name in sys.modules if is_isolated_name(name)} != (
            expected_modules | expected_namespaces
        ):
            raise PinnedRuntimeDriftError
        for name, module, source_path in self._isolated_modules:
            if sys.modules.get(name) is not module:
                raise PinnedRuntimeDriftError
            current_file = getattr(module, "__file__", None)
            if not isinstance(current_file, str) or Path(current_file).resolve() != source_path:
                raise PinnedRuntimeDriftError
        if any(sys.modules.get(name) is not module for name, module in self._namespace_modules):
            raise PinnedRuntimeDriftError
        if any(
            getattr(module, "logger", None) is not logger for module, logger in self._logger_globals
        ):
            raise PinnedRuntimeDriftError

    def snapshot(self) -> RuntimeGuardSnapshot:
        """Return safe counts after one successful observation."""
        self.observe()
        return RuntimeGuardSnapshot(
            logger_count=len(self._loggers),
            isolated_module_count=len(self._isolated_modules),
            forbidden_module_count=0,
        )


def approved_module_names() -> tuple[str, ...]:
    """Return the exact isolated source-module names in load order."""
    return tuple(f"{ISOLATED_NAMESPACE}.{relative}" for relative in MODULE_ORDER)


def isolated_namespace_names(module_names: tuple[str, ...]) -> tuple[str, ...]:
    """Return namespace containers only when isolated sources are expected."""
    if not module_names:
        return ()
    return (
        ISOLATED_NAMESPACE,
        f"{ISOLATED_NAMESPACE}._backends",
        f"{ISOLATED_NAMESPACE}._async",
    )


def is_isolated_name(name: str) -> bool:
    """Return whether a module name belongs to the private namespace."""
    return name == ISOLATED_NAMESPACE or name.startswith(f"{ISOLATED_NAMESPACE}.")


def seal_isolated_runtime(
    module_names: tuple[str, ...],
    module_objects: tuple[ModuleType, ...],
    namespace_objects: tuple[ModuleType, ...],
) -> None:
    """Seal the locally loaded approved objects before any guard can re-baseline them."""
    global _canonical_isolated_modules  # noqa: PLW0603 - one runtime identity seal.
    global _canonical_namespace_modules  # noqa: PLW0603 - one runtime identity seal.
    namespace_names = isolated_namespace_names(module_names)
    source_paths = _isolated_source_paths(module_names)
    if len(module_names) != len(module_objects) or len(namespace_names) != len(namespace_objects):
        raise PinnedRuntimeDriftError
    modules = tuple(
        (name, module, path)
        for name, module, path in zip(module_names, module_objects, source_paths, strict=True)
    )
    namespaces = tuple(zip(namespace_names, namespace_objects, strict=True))
    if any(sys.modules.get(name) is not module for name, module, _path in modules):
        raise PinnedRuntimeDriftError
    if any(sys.modules.get(name) is not module for name, module in namespaces):
        raise PinnedRuntimeDriftError
    if _canonical_isolated_modules is None and _canonical_namespace_modules is None:
        _canonical_isolated_modules = modules
        _canonical_namespace_modules = namespaces
        return
    canonical_modules = _canonical_isolated_modules
    canonical_namespaces = _canonical_namespace_modules
    if canonical_modules is None or canonical_namespaces is None:
        raise PinnedRuntimeDriftError
    if len(canonical_modules) != len(modules) or any(
        current_name != sealed_name or current_module is not sealed_module
        for (current_name, current_module, _path), (
            sealed_name,
            sealed_module,
            _sealed_path,
        ) in zip(
            modules,
            canonical_modules,
            strict=True,
        )
    ):
        raise PinnedRuntimeDriftError
    if len(canonical_namespaces) != len(namespaces) or any(
        current_name != sealed_name or current_module is not sealed_module
        for (current_name, current_module), (sealed_name, sealed_module) in zip(
            namespaces,
            canonical_namespaces,
            strict=True,
        )
    ):
        raise PinnedRuntimeDriftError


def _canonical_isolated_state(
    module_names: tuple[str, ...],
) -> tuple[
    tuple[tuple[str, ModuleType, Path], ...],
    tuple[tuple[str, ModuleType], ...],
]:
    if not module_names:
        return (), ()
    modules = _canonical_isolated_modules
    namespaces = _canonical_namespace_modules
    if modules is None or namespaces is None:
        raise PinnedRuntimeDriftError
    if tuple(name for name, _module, _path in modules) != module_names:
        raise PinnedRuntimeDriftError
    return modules, namespaces


def _isolated_source_paths(module_names: tuple[str, ...]) -> tuple[Path, ...]:
    if not module_names:
        return ()
    distribution = importlib.metadata.distribution("httpcore2")
    source_root = Path(str(distribution.locate_file("httpcore2"))).resolve()
    paths: list[Path] = []
    prefix = f"{ISOLATED_NAMESPACE}."
    for name in module_names:
        if not name.startswith(prefix):
            raise PinnedRuntimeDriftError
        relative = name.removeprefix(prefix)
        path = source_root.joinpath(*relative.split(".")).with_suffix(".py").resolve()
        if source_root not in path.parents or path.is_symlink():
            raise PinnedRuntimeDriftError
        paths.append(path)
    return tuple(paths)


def _capture_logger_globals(
    isolated_module_names: tuple[str, ...],
    loggers: dict[str, FrozenNoOpLogger],
) -> tuple[tuple[ModuleType, FrozenNoOpLogger], ...]:
    bindings: list[tuple[ModuleType, FrozenNoOpLogger]] = []
    httpx_client = sys.modules.get("httpx2._client")
    if isinstance(httpx_client, ModuleType):
        bindings.append((httpx_client, loggers["httpx2"]))
    suffixes = (
        ("._async.connection", "httpcore2.connection"),
        ("._async.http11", "httpcore2.http11"),
    )
    for suffix, logger_name in suffixes:
        matches = tuple(name for name in isolated_module_names if name.endswith(suffix))
        if isolated_module_names and len(matches) != 1:
            raise PinnedRuntimeDriftError
        for name in matches:
            module = sys.modules.get(name)
            if not isinstance(module, ModuleType):
                raise PinnedRuntimeDriftError
            bindings.append((module, loggers[logger_name]))
    expected = len(suffixes) + int(httpx_client is not None)
    if isolated_module_names and len(bindings) != expected:
        raise PinnedRuntimeDriftError
    return tuple(bindings)
