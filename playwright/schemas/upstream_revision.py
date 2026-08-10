from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class UpstreamConclusionRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_request_id: str = Field(min_length=1)
    final_conclusion_id: str = Field(min_length=1)
    affected_claim_ids: list[str] = Field(default_factory=list)
    affected_evidence_ids: list[str] = Field(default_factory=list)
    issue_type: str = Field(min_length=1)
    issue_description: str = Field(min_length=1)
    required_resolution: str = Field(min_length=1)
    acceptance_conditions: list[str] = Field(min_length=1)
    source_finding_ids: list[str] = Field(min_length=1)

