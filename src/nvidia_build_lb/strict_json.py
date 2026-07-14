"""Small strict duplicate-key-rejecting JSON loader for local protocols."""

import json
from collections.abc import Callable
from typing import Protocol, runtime_checkable


@runtime_checkable
class _JsonObjectLoader(Protocol):
    def __call__(
        self,
        value: str,
        *,
        object_pairs_hook: Callable[[list[tuple[str, object]]], dict[str, object]],
    ) -> object:
        """Parse JSON through the exact injected object hook."""
        ...


def load_unique_json(value: str) -> object:
    """Load one JSON value while rejecting duplicate object names."""
    loader = _erase_type(json.loads)
    if not isinstance(loader, _JsonObjectLoader):
        raise TypeError
    return loader(value, object_pairs_hook=_unique)


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, item in pairs:
        if name in result:
            raise ValueError
        result[name] = item
    return result


def _erase_type(value: object) -> object:
    return value
