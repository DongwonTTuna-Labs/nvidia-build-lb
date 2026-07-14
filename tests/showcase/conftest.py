from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from nvidia_build_lb.main import create_app


@pytest.fixture
def showcase_client() -> Generator[TestClient]:
    with TestClient(create_app(), base_url="http://127.0.0.1:2456") as client:
        yield client
