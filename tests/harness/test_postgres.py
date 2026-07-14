"""Green unit checks for the no-Docker lazy PostgreSQL harness state machine."""

from typing import ClassVar, Self

import pytest

from .postgres import LazyPostgresHarness, PostgresContainerLike


class _FakeContainer:
    __slots__: ClassVar[tuple[str, ...]] = ("start_count", "stop_count")

    start_count: int
    stop_count: int

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0

    def start(self) -> Self:
        self.start_count += 1
        return self

    def stop(self) -> None:
        self.stop_count += 1

    def get_connection_url(self) -> str:
        return "postgresql+asyncpg://synthetic@127.0.0.1/synthetic"


class _FakeFactory:
    __slots__: ClassVar[tuple[str, ...]] = ("call_count", "container")

    container: _FakeContainer
    call_count: int

    def __init__(self, container: _FakeContainer) -> None:
        self.container = container
        self.call_count = 0

    def __call__(self, image: str) -> PostgresContainerLike:
        assert image == "postgres:17-alpine"
        self.call_count += 1
        return self.container


def test_postgres_harness_construction_does_not_invoke_factory() -> None:
    factory = _FakeFactory(container=_FakeContainer())
    harness = LazyPostgresHarness(factory=factory)

    assert harness.started is False
    assert factory.call_count == 0
    with pytest.raises(RuntimeError):
        _ = harness.connection_url()


def test_postgres_harness_start_and_stop_are_idempotent() -> None:
    container = _FakeContainer()
    factory = _FakeFactory(container=container)
    harness = LazyPostgresHarness(factory=factory)

    harness.start()
    harness.start()

    assert harness.started is True
    assert factory.call_count == 1
    assert container.start_count == 1
    assert harness.connection_url().get_secret_value().endswith("/synthetic")

    harness.stop()
    harness.stop()

    assert harness.started is False
    assert container.stop_count == 1


def test_postgres_harness_context_always_stops_injected_fake() -> None:
    container = _FakeContainer()
    harness = LazyPostgresHarness(factory=_FakeFactory(container=container))

    with harness as entered:
        assert entered.started is True

    assert harness.started is False
    assert container.stop_count == 1
