"""Preserved historical v15 design artifact integrity."""

import hashlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.nvidia_routing

_CONTRACT = Path(__file__).resolve().parents[1] / "fixtures/nvidia-routing-v15/contract-v15.json"
_CONTRACT_SHA256 = "38ba64a99cc53873b043ae56c62eb3f23a9573d25246ceac25efcf1482c1cebe"


def test_historical_design_artifact_hash_is_preserved() -> None:
    assert hashlib.sha256(_CONTRACT.read_bytes()).hexdigest() == _CONTRACT_SHA256
