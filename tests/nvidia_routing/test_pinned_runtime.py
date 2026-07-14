"""Current-runtime closure and isolated H1 loader verification."""

import logging
import subprocess
import sys
from types import ModuleType
from typing import Protocol, override, runtime_checkable

import pytest

from nvidia_build_lb import runtime_manifest
from nvidia_build_lb.pinned_runtime import (
    PINNED_RUNTIME,
    FrozenNoOpLogger,
    PinnedRuntimeDriftError,
    SealedLoggerMutationError,
    TransportBoundaryGuard,
    load_isolated_runtime,
    provider_logger,
    verify_runtime_closure,
)
from nvidia_build_lb.runtime_logger import logger_registry
from nvidia_build_lb.sse import MAX_SSE_TRANSPORT_CHUNK_BYTES

pytestmark = pytest.mark.nvidia_routing


def test_stable_record_digest_ignores_only_environment_generated_launchers() -> None:
    host = (
        b"../../../bin/httpx2,sha256=host,351\n"
        b"httpx2/_client.py,sha256=source,42\n"
        b"httpx2-2.5.0.dist-info/RECORD,,\n"
    )
    container = host.replace(b"sha256=host,351", b"sha256=container,301")
    changed_source = host.replace(b"sha256=source,42", b"sha256=changed,42")

    assert runtime_manifest.stable_record_digest(host) == runtime_manifest.stable_record_digest(
        container
    )
    assert runtime_manifest.stable_record_digest(host) != runtime_manifest.stable_record_digest(
        changed_source
    )


@runtime_checkable
class _LoggerModule(Protocol):
    logger: object


def test_runtime_receipt_matches_approved_versions_records_and_closure() -> None:
    repeated = verify_runtime_closure()

    assert repeated == PINNED_RUNTIME
    assert repeated.distribution_count == 3
    assert repeated.python_source_count == 66
    assert repeated.closure_count == 47
    assert repeated.closure_merkle_sha256 == (
        "70370d8b264ad3dbae6079bec1df15a0cdffbb3e921aeaec5bf722f97438c505"
    )
    assert repeated.logger_count == 3
    assert repeated.non_provider_hash_count == 3


def test_isolated_loader_executes_only_approved_h1_modules() -> None:
    runtime = load_isolated_runtime()

    assert len(runtime.module_names) == 13
    assert all(name in sys.modules for name in runtime.module_names)
    assert not any(name == "httpcore2" or name.startswith("httpcore2.") for name in sys.modules)
    snapshot = TransportBoundaryGuard(runtime.module_names).snapshot()
    assert snapshot.logger_count == 3
    assert snapshot.isolated_module_count == 13
    assert snapshot.forbidden_module_count == 0


def test_sse_parser_chunk_bound_matches_the_pinned_h1_transport_read_bound() -> None:
    runtime = load_isolated_runtime()
    module_name = next(name for name in runtime.module_names if name.endswith("._async.http11"))
    module: object = sys.modules[module_name]
    connection_type = getattr(module, "AsyncHTTP11Connection", None)
    read_bound = getattr(connection_type, "READ_NUM_BYTES", None)

    assert isinstance(read_bound, int)
    assert read_bound == MAX_SSE_TRANSPORT_CHUNK_BYTES


def test_provider_loggers_are_noop_and_reject_mutation_without_argument_access() -> None:
    for name in ("httpx2", "httpcore2.connection", "httpcore2.http11"):
        logger = provider_logger(name)
        assert logger.disabled is True
        assert logger.handlers == ()
        assert logger.propagate is False
        logger.info(object())
        with pytest.raises(SealedLoggerMutationError):
            logger.setLevel(logging.DEBUG)


def test_boundary_guard_detects_direct_logger_replacement_before_operation() -> None:
    registry = logging.Logger.manager.loggerDict
    original = registry["httpx2"]
    registry["httpx2"] = logging.getLogger("replacement-httpx2")
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            TransportBoundaryGuard().observe()
    finally:
        registry["httpx2"] = original


def test_new_boundary_guard_cannot_rebaseline_noop_logger_subclass_replacement() -> None:
    class _ReplacementLogger(FrozenNoOpLogger):
        @override
        def info(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError

    registry = logger_registry()
    original = registry.get("httpx2")
    assert isinstance(original, FrozenNoOpLogger)
    replacement = _ReplacementLogger("httpx2")
    registry["httpx2"] = replacement
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            TransportBoundaryGuard().observe()
    finally:
        registry["httpx2"] = original


def test_runtime_load_rejects_logger_replacement_before_first_isolated_load() -> None:
    program = """
import logging
from nvidia_build_lb import pinned_runtime

class Replacement(pinned_runtime.FrozenNoOpLogger):
    def info(self, *_args, **_kwargs):
        raise AssertionError

logging.Logger.manager.loggerDict["httpx2"] = Replacement("httpx2")
try:
    pinned_runtime.load_isolated_runtime()
except pinned_runtime.PinnedRuntimeDriftError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and inline test program.
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 0, (completed.stdout, completed.stderr)


def test_runtime_reload_cannot_rebaseline_same_path_module_replacement() -> None:
    runtime = load_isolated_runtime()
    module_name = runtime.module_names[-1]
    original = sys.modules[module_name]
    replacement = ModuleType(module_name)
    replacement.__file__ = original.__file__
    sys.modules[module_name] = replacement
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            _ = load_isolated_runtime()
    finally:
        sys.modules[module_name] = original
    TransportBoundaryGuard(runtime.module_names).observe()


def test_boundary_guard_detects_isolated_module_identity_replacement() -> None:
    runtime = load_isolated_runtime()
    guard = TransportBoundaryGuard(runtime.module_names)
    module_name = runtime.module_names[-1]
    original = sys.modules[module_name]
    sys.modules[module_name] = ModuleType(module_name)
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            guard.observe()
    finally:
        sys.modules[module_name] = original
    guard.observe()


def test_boundary_guard_detects_approved_module_logger_replacement() -> None:
    runtime = load_isolated_runtime()
    guard = TransportBoundaryGuard(runtime.module_names)
    module_name = next(name for name in runtime.module_names if name.endswith("._async.http11"))
    module_object: object = sys.modules[module_name]
    assert isinstance(module_object, _LoggerModule)
    module = module_object
    original = module.logger
    module.logger = object()
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            guard.observe()
    finally:
        module.logger = original
    guard.observe()


def test_boundary_guard_detects_forbidden_normal_provider_import() -> None:
    runtime = load_isolated_runtime()
    guard = TransportBoundaryGuard(runtime.module_names)
    module_name = "httpcore2._async.http2"
    sys.modules[module_name] = ModuleType(module_name)
    try:
        with pytest.raises(PinnedRuntimeDriftError):
            guard.observe()
    finally:
        del sys.modules[module_name]
    guard.observe()
