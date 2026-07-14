"""Hash-approved isolated execution loader for the httpcore2 H1 subset."""

import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from nvidia_build_lb.runtime_guard import (
    TransportBoundaryGuard,
    approved_module_names,
    is_isolated_name,
    isolated_namespace_names,
    seal_isolated_runtime,
)
from nvidia_build_lb.runtime_manifest import verify_runtime_closure
from nvidia_build_lb.runtime_manifest_data import ISOLATED_NAMESPACE, MODULE_ORDER
from nvidia_build_lb.runtime_types import IsolatedRuntime, PinnedRuntimeDriftError


def load_isolated_runtime() -> IsolatedRuntime:
    """Execute approved httpcore2 H1 sources in one private namespace."""
    existing = _loaded_isolated_runtime()
    if existing is not None:
        return existing
    TransportBoundaryGuard().observe()
    _ = verify_runtime_closure()
    distribution = importlib.metadata.distribution("httpcore2")
    source_root = Path(str(distribution.locate_file("httpcore2"))).resolve()
    namespaces = (
        _create_namespace(ISOLATED_NAMESPACE, source_root),
        _create_namespace(f"{ISOLATED_NAMESPACE}._backends", source_root / "_backends"),
        _create_namespace(f"{ISOLATED_NAMESPACE}._async", source_root / "_async"),
    )
    loaded: dict[str, ModuleType] = {}
    for relative in MODULE_ORDER:
        module_name = f"{ISOLATED_NAMESPACE}.{relative}"
        source_path = source_root.joinpath(*relative.split(".")).with_suffix(".py").resolve()
        if source_root not in source_path.parents or source_path.is_symlink():
            raise PinnedRuntimeDriftError
        specification = importlib.util.spec_from_file_location(module_name, source_path)
        if specification is None or specification.loader is None:
            raise PinnedRuntimeDriftError
        module = importlib.util.module_from_spec(specification)
        sys.modules[module_name] = module
        try:
            specification.loader.exec_module(module)
        except Exception:  # noqa: BLE001 - loader failures become one safe drift code.
            _ = sys.modules.pop(module_name, None)
            raise PinnedRuntimeDriftError from None
        loaded[relative] = module
    module_names = approved_module_names()
    runtime = IsolatedRuntime(
        models=loaded["_models"],
        exceptions=loaded["_exceptions"],
        anyio_backend=loaded["_backends.anyio"],
        connection_pool=loaded["_async.connection_pool"],
        module_names=module_names,
    )
    seal_isolated_runtime(
        module_names,
        tuple(loaded[relative] for relative in MODULE_ORDER),
        namespaces,
    )
    TransportBoundaryGuard(module_names).observe()
    return runtime


def _loaded_isolated_runtime() -> IsolatedRuntime | None:
    module_names = approved_module_names()
    loaded_names = {name for name in sys.modules if is_isolated_name(name)}
    expected_names = set(module_names) | set(isolated_namespace_names(module_names))
    if not loaded_names:
        return None
    if loaded_names != expected_names:
        raise PinnedRuntimeDriftError
    TransportBoundaryGuard(module_names).observe()
    return IsolatedRuntime(
        models=sys.modules[module_names[2]],
        exceptions=sys.modules[module_names[0]],
        anyio_backend=sys.modules[module_names[7]],
        connection_pool=sys.modules[module_names[-1]],
        module_names=module_names,
    )


def _create_namespace(name: str, path: Path) -> ModuleType:
    module = ModuleType(name)
    module.__package__ = name
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module
