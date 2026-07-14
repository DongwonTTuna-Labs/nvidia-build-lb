"""Safe errors and typed receipts for the pinned provider runtime."""

from dataclasses import dataclass
from types import ModuleType
from typing import override

_RUNTIME_DRIFT_ERROR = "pinned_runtime_drift"
_LOGGER_MUTATION_ERROR = "sealed_logger_mutation"


class PinnedRuntimeDriftError(Exception):
    """Fail closed when runtime identity drifts."""

    @override
    def __str__(self) -> str:
        return _RUNTIME_DRIFT_ERROR


class SealedLoggerMutationError(Exception):
    """Reject mutation of one provider-library logger."""

    @override
    def __str__(self) -> str:
        return _LOGGER_MUTATION_ERROR


@dataclass(frozen=True, slots=True)
class RuntimeReceipt:
    """Secret-free proof that the approved runtime closure is current."""

    distribution_count: int
    python_source_count: int
    closure_count: int
    closure_merkle_sha256: str
    logger_count: int
    non_provider_hash_count: int


@dataclass(frozen=True, slots=True)
class IsolatedRuntime:
    """Only the isolated modules needed by the H1 transport."""

    models: ModuleType
    exceptions: ModuleType
    anyio_backend: ModuleType
    connection_pool: ModuleType
    module_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeGuardSnapshot:
    """Safe runtime identity counts for readiness and tests."""

    logger_count: int
    isolated_module_count: int
    forbidden_module_count: int
