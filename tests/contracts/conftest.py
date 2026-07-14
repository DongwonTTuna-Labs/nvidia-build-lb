"""Import-safe TestClient fixture for intentional-red contracts."""

from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nvidia_build_lb.main import create_app

from ._support import ContractClient
from .fakes import contract_services

_CONTRACT_ROOT = Path(__file__).parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Include the now-green locked wire contracts in the Todo 5 API gate."""
    for item in items:
        if Path(item.path).is_relative_to(_CONTRACT_ROOT):
            item.add_marker(pytest.mark.api)


@pytest.fixture
def contract_client() -> Generator[ContractClient]:
    """Create a no-socket client and publish only synthetic future seed state."""
    services, readiness = contract_services()
    app = create_app(services)
    with TestClient(app, base_url="http://127.0.0.1:2456") as http:
        yield ContractClient(app=app, http=http, readiness=readiness)
