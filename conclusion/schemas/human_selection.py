from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SelectionType(str, Enum):
    CANDIDATE = "candidate"
    INTEGRATED_OPTION = "integrated_option"
    MULTI_CANDIDATE_INTEGRATION = "multi_candidate_integration"
    DEFER = "defer"


class HumanSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    selection_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    selected_candidate_ids: list[str] = Field(default_factory=list)
    selection_type: SelectionType
    user_instruction: str | None = None
    accepted_tradeoffs: list[str] = Field(default_factory=list)
    accepted_limitations: list[str] = Field(default_factory=list)
    rejected_candidate_ids: list[str] = Field(default_factory=list)
    selected_at: datetime = Field(default_factory=utc_now)
