"""Shared strict base for administration data-transfer objects."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class AdminDTO(BaseModel):
    """Forbid implicit coercion, mutation, and undeclared wire fields."""

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
    )
