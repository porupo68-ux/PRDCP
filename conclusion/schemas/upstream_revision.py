from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UpstreamDeliberationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_request_id: str = Field(min_length=1)
    affected_candidate_ids: list[str] = Field(default_factory=list)
    affected_claim_ids: list[str] = Field(default_factory=list)
    missing_analysis_description: str = Field(min_length=1)
    required_analysis_types: list[str] = Field(min_length=1)
    acceptance_conditions: list[str] = Field(min_length=1)
    source_finding_ids: list[str] = Field(min_length=1)
