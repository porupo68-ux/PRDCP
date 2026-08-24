from __future__ import annotations

from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel


OutputT = TypeVar("OutputT", bound=BaseModel)


class ModelProvider(Protocol):
    provider_id: str
    reservation_root: Path | None

    async def generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[OutputT],
        timeout_seconds: int | None = None,
        invocation_reservation_path: Path | None = None,
    ) -> dict:
        ...
