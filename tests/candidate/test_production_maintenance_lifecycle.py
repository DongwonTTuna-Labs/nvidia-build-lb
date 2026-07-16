"""Production maintenance join and engine-disposal ownership contracts."""

from uuid import UUID

import anyio
import pytest
from pydantic import SecretBytes, SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import nvidia_build_lb.service_epoch as service_epoch_module
from nvidia_build_lb import production_graph
from nvidia_build_lb.config import DeploymentStage, LogLevel, Settings
from nvidia_build_lb.main import create_app
from nvidia_build_lb.production_graph import ProductionResources
from nvidia_build_lb.runtime_readiness import RuntimeReadinessGate
from nvidia_build_lb.service_epoch import ServiceEpochRuntime
from nvidia_build_lb.service_epoch_monitor import CleanStopState
from nvidia_build_lb.transport_adapter import SanitizedAsyncClient
from tests.contracts.fakes import contract_services

pytestmark = pytest.mark.anyio

_EPOCH = UUID("00000000-0000-4000-8000-000000000511")
_ASSEMBLY_FAILURE = "injected_app_assembly_failure"


def _settings() -> Settings:
    return Settings(
        database_url=SecretStr("postgresql+asyncpg://nvidia_build_lb@db/nvidia_build_lb"),
        vault_key=SecretBytes(b"v" * 32),
        admin_token=SecretStr(f"nblb_admin_{'a' * 64}"),
        stage=DeploymentStage.PRODUCTION,
        log_level=LogLevel.INFO,
        admin_read_deadline_seconds=5,
        admin_mutation_deadline_seconds=125,
        admin_event_retention_days=30,
        admin_event_max_rows=100_000,
        admin_attempt_max_rows=40_000,
        admin_ledger_prune_batch_size=1_000,
        admin_ledger_maintenance_interval_seconds=300,
        admin_attempt_reconciliation_grace_seconds=300,
    )


class _EpochConnection:
    autocommit: bool = True
    dedicated: bool = True
    driver: str = "psycopg_async"
    pool: str = "none"
    events: list[str]

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def try_advisory_lock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        return True

    async def delete_prior_epoch_pins(self, active_epoch: UUID) -> int:
        del active_epoch
        return 0

    async def ping(self) -> bool:
        return True

    async def advisory_unlock(self, first_key: int, second_key: int) -> bool:
        del first_key, second_key
        self.events.append("epoch-unlock")
        return True

    async def close(self) -> None:
        self.events.append("epoch-close")


def _epoch(connection: _EpochConnection) -> ServiceEpochRuntime:
    joined = anyio.Event()
    joined.set()
    return ServiceEpochRuntime(
        epoch=_EPOCH,
        connection=connection,
        clean_stop=CleanStopState(),
        monitor_scope=anyio.CancelScope(),
        monitor_joined=joined,
    )


async def test_maintenance_join_timeout_skips_engine_dispose_but_runs_safe_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(production_graph, "_MAINTENANCE_JOIN_TIMEOUT_SECONDS", 0.01)
    events: list[str] = []
    engine = create_async_engine("postgresql+asyncpg://localhost/nvidia_build_lb_test")
    original_dispose = AsyncEngine.dispose
    engine_disposed = False

    async def record_dispose(candidate: AsyncEngine, close: bool = True) -> None:
        del candidate, close
        nonlocal engine_disposed
        engine_disposed = True

    monkeypatch.setattr(AsyncEngine, "dispose", record_dispose)
    client = SanitizedAsyncClient.create()
    readiness = RuntimeReadinessGate()
    readiness.set_ready(True)
    maintenance_joined = anyio.Event()
    maintenance_scope = anyio.CancelScope()
    services, _ = contract_services()
    resources = ProductionResources(
        app=create_app(),
        engine=engine,
        client=client,
        epoch=_epoch(_EpochConnection(events)),
        readiness=readiness,
        logger=services.logger,
        maintenance_scope=maintenance_scope,
        maintenance_joined=maintenance_joined,
    )

    try:
        with pytest.raises(RuntimeError, match="production_cleanup_failed"):
            await resources.close()

        assert readiness.is_ready() is False
        assert maintenance_scope.cancel_called
        assert maintenance_joined.is_set() is False
        assert events == ["epoch-unlock", "epoch-close"]
        assert engine_disposed is False
    finally:
        await original_dispose(engine)


async def test_graph_assembly_failure_retires_every_started_resource_in_order(  # noqa: C901
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class _Engine:
        async def dispose(self) -> None:
            events.append("engine-dispose")

    class _Client:
        async def aclose(self) -> None:
            events.append("client-close")

    class _ClientFactory:
        @staticmethod
        def create() -> _Client:
            return _Client()

    class _MaintenanceScope:
        def cancel(self) -> None:
            events.append("maintenance-cancel")

    class _MaintenanceJoined:
        async def wait(self) -> None:
            events.append("maintenance-join")

    runtime = _epoch(_EpochConnection(events))

    class _Coordinator:
        async def start(self, *_args: object) -> ServiceEpochRuntime:
            return runtime

    async def bound(_sessions: object, _vault: object) -> None:
        return None

    async def start_maintenance(*_args: object) -> tuple[object, object]:
        return _MaintenanceScope(), _MaintenanceJoined()

    def fail_app(_services: object) -> None:
        raise RuntimeError(_ASSEMBLY_FAILURE)

    def create_test_engine(_url: SecretStr) -> _Engine:
        return engine

    def create_test_sessions(_engine: object) -> object:
        return object()

    def create_test_adapter(_dependencies: object) -> object:
        return object()

    def create_test_coordinator(_dependencies: object) -> _Coordinator:
        return _Coordinator()

    engine = _Engine()
    monkeypatch.setattr(production_graph, "create_engine", create_test_engine)
    monkeypatch.setattr(production_graph, "create_session_factory", create_test_sessions)
    monkeypatch.setattr(production_graph, "ensure_vault_key_binding", bound)
    monkeypatch.setattr(production_graph, "SanitizedAsyncClient", _ClientFactory)
    monkeypatch.setattr(production_graph, "NvidiaHostedAdapter", create_test_adapter)
    monkeypatch.setattr(
        service_epoch_module,
        "ServiceEpochCoordinator",
        create_test_coordinator,
    )
    monkeypatch.setattr(production_graph, "start_maintenance_worker", start_maintenance)
    monkeypatch.setattr(production_graph, "create_app", fail_app)

    observed: RuntimeError | None = None
    async with anyio.create_task_group() as tasks:
        with anyio.CancelScope() as root_scope:
            try:
                _ = await production_graph.build_production_resources(
                    _settings(),
                    tasks,
                    root_scope,
                )
            except RuntimeError as error:
                observed = error

    assert observed is not None
    assert str(observed) == _ASSEMBLY_FAILURE

    assert events == [
        "maintenance-cancel",
        "maintenance-join",
        "epoch-unlock",
        "epoch-close",
        "client-close",
        "engine-dispose",
    ]
