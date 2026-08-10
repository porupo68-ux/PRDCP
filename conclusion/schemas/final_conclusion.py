from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FinalConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_conclusion_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    conclusion_package_id: str = Field(min_length=1)
    human_selection_id: str = Field(min_length=1)
    selected_position: dict[str, Any]
    final_recommendation: str = Field(min_length=1)
    implementation_direction: list[str] = Field(min_length=1)
    responsible_actors: list[str] = Field(min_length=1)
    expected_benefits: list[str] = Field(default_factory=list)
    accepted_tradeoffs: list[str] = Field(default_factory=list)
    accepted_risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    supporting_claim_ids: list[str] = Field(min_length=1)
    supporting_analysis_ids: list[str] = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(min_length=1)
    supporting_source_ids: list[str] = Field(min_length=1)
    rejected_alternatives_summary: list[dict[str, Any]] = Field(default_factory=list)
    selection_authority: str = Field(default="user", pattern=r"^user$")
    finalized_at: datetime = Field(default_factory=utc_now)
