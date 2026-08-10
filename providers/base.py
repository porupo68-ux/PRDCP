from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel


OutputT = TypeVar("OutputT", bound=BaseModel)


class ModelProvider(Protocol):
    async def generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[OutputT],
    ) -> dict:
        ...

