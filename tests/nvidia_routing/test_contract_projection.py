"""Independent v15 semantic-mutation rejection projection."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.nvidia_routing

_DIRECTORY = Path(__file__).resolve().parents[1] / "fixtures/nvidia-routing-v15"
_CONTRACT = _DIRECTORY / "contract-v15.json"
_VERIFIER = _DIRECTORY / "verify-v15-contract.py.snapshot"
_CONTRACT_SHA256 = "38ba64a99cc53873b043ae56c62eb3f23a9573d25246ceac25efcf1482c1cebe"
_VERIFIER_SHA256 = "dcb75c561f066a4317c3a2431ef9ce58b50f51badec1111196d38f75c45c0fb9"
_MUTATIONS = (
    "stale_version",
    "coherent_selection_change",
    "axis_input_oracle_added",
    "axis_entrypoint_changed",
    "axis_owner_changed",
    "executable_oracle_changed",
    "release_count_changed",
    "release_trace_changed",
    "production_checkpoint_changed",
    "production_trace_changed",
    "cleanup_trace_changed",
    "cleanup_state_changed",
    "cleanup_owner_changed",
    "receipt_failure_exit_changed",
    "release_dispatch_changed",
    "checkpoint_declaration_changed",
    "last_database_state_changed",
    "handoff_exit_changed",
    "receipt_readback_changed",
    "runtime_hash_changed",
    "predecessor_hash_changed",
    "missing_atomic_assertion",
)
_PROBE = """
import hashlib, json, sys
from pathlib import Path
contract = Path(sys.argv[1])
verifier = Path(sys.argv[2])
expected = sys.argv[3]
verifier_expected = sys.argv[4]
raw = contract.read_bytes()
verifier_raw = verifier.read_bytes()
if hashlib.sha256(raw).hexdigest() != expected:
    raise SystemExit("contract_fixture_hash_mismatch")
if hashlib.sha256(verifier_raw).hexdigest() != verifier_expected:
    raise SystemExit("verifier_fixture_hash_mismatch")
namespace = {"__name__": "v15_projection_probe"}
exec(compile(verifier_raw, str(verifier), "exec"), namespace)
document = json.loads(raw)
for row in namespace["semantic_mutation_rows"](document):
    assert row["expected_failure_locations"]
    print(row["name"])
"""


@pytest.fixture(scope="module")
def rejected_mutations() -> frozenset[str]:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and local reviewed files.
        [
            sys.executable,
            "-c",
            _PROBE,
            str(_CONTRACT),
            str(_VERIFIER),
            _CONTRACT_SHA256,
            _VERIFIER_SHA256,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return frozenset(completed.stdout.splitlines())


@pytest.mark.parametrize("mutation", _MUTATIONS, ids=_MUTATIONS)
def test_semantic_mutation(mutation: str, rejected_mutations: frozenset[str]) -> None:
    assert mutation in rejected_mutations
