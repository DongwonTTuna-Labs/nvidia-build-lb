import importlib.util

import pytest

pytestmark = pytest.mark.ui_fake


def test_same_contract_fake_state_and_server_seams_exist_for_browser_qa() -> None:
    # Given: Todo 4 must not compose or mutate the production application root.
    module_names = ("tests.ui.fake_admin_state", "tests.ui.fake_admin_server")

    # When: the two isolated QA seams are resolved.
    specifications = tuple(importlib.util.find_spec(name) for name in module_names)

    # Then: deterministic state and HTTP delivery are available only under tests.
    assert all(specification is not None for specification in specifications)
