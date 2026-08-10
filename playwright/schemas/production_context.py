from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    production_context_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    final_conclusion_id: str = Field(min_length=1)
    conclusion_package_id: str = Field(min_length=1)
    human_selection_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    central_question: str = Field(min_length=1)
    selected_position: dict[str, Any]
    final_recommendation: str = Field(min_length=1)
    target_audience: str = Field(min_length=1)
    video_objective: str = Field(min_length=1)
    desired_duration_seconds: int = Field(ge=60, le=7200)
    language: str = Field(default="ja", min_length=1)
    format: str = Field(default="YouTube解説動画", min_length=1)
    must_include_claim_ids: list[str] = Field(min_length=1)
    must_include_evidence_ids: list[str] = Field(min_length=1)
    accepted_tradeoffs: list[str] = Field(default_factory=list)
    accepted_risks: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    limitations_to_disclose: list[str] = Field(default_factory=list)
    tone_constraints: list[str] = Field(default_factory=list)
    format_constraints: list[str] = Field(default_factory=list)
    source_manifest: list[dict[str, Any]] = Field(min_length=1)

    @model_validator(mode="after")
    def require_unique_references(self) -> "ProductionContext":
        if len(self.must_include_claim_ids) != len(set(self.must_include_claim_ids)):
            raise ValueError("must_include_claim_ids must be unique")
        if len(self.must_include_evidence_ids) != len(set(self.must_include_evidence_ids)):
            raise ValueError("must_include_evidence_ids must be unique")
        return self

