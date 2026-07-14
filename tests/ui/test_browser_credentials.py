import json
from typing import final

import pytest

from .browser_checks import BrowserAssertionError
from .browser_credentials import assert_secret_absent


@final
class _SyntheticPage:
    def __init__(self, observation: dict[str, bool]) -> None:
        self._observation: dict[str, bool] = observation

    def evaluate(self, expression: str, arg: str) -> str:
        del expression, arg
        return json.dumps(self._observation)


def test_secret_absence_failure_names_only_failed_surfaces() -> None:
    sentinel = "synthetic-secret-never-rendered"
    page = _SyntheticPage(
        {
            "rendered_text_absent": True,
            "serialized_dom_absent": False,
            "attribute_values_absent": True,
            "form_values_absent": False,
            "fragments_absent": False,
        }
    )

    with pytest.raises(
        BrowserAssertionError,
        match=(
            r"^credential remained in browser state surfaces: "
            "serialized_dom_absent,form_values_absent,fragments_absent$"
        ),
    ) as caught:
        assert_secret_absent(page, sentinel)

    assert sentinel not in str(caught.value)


def test_secret_absence_passes_when_every_surface_is_clear() -> None:
    page = _SyntheticPage(
        {
            "rendered_text_absent": True,
            "serialized_dom_absent": True,
            "attribute_values_absent": True,
            "form_values_absent": True,
            "fragments_absent": True,
        }
    )

    assert_secret_absent(page, "synthetic-secret-never-rendered")
