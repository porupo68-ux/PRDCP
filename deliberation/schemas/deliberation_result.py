from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deliberation.schemas.integrated_analysis import TraceabilityEntry, Viewpoint
from deliberation.schemas.review import DeliberationQualityReviewOutput
from researcher.schemas.human_evidence import AcceptedEvidenceGap, HumanEvidenceDecision


class DeliberationResult(BaseModel):
    """Deliberation artifact plus the canonical Deliberation→Conclusion contract."""

    model_config = ConfigDict(extra="forbid")

    deliberation_result_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    research_report_id: str = Field(min_length=1)
    research_plan_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    general_opinion: str = Field(min_length=1)
    problem_definition: dict[str, Any]
    claim_structure: list[dict[str, Any]] = Field(min_length=1)
    key_assumptions: list[dict[str, Any]] = Field(default_factory=list)
    evidence_relationships: list[dict[str, Any]] = Field(min_length=1)
    causal_model: dict[str, Any]
    structural_factors: list[dict[str, Any]] = Field(min_length=1)
    stakeholder_structure: dict[str, Any]
    existing_response_evaluation: list[dict[str, Any]] = Field(default_factory=list)
    counterarguments: list[dict[str, Any]] = Field(min_length=1)
    alternative_interpretations: list[dict[str, Any]] = Field(default_factory=list)
    trade_offs: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    analysis_perspectives: list[Viewpoint] = Field(min_length=1, max_length=3)
    unresolved_issues: list[dict[str, Any]] = Field(default_factory=list)
    research_gaps: list[dict[str, Any]] = Field(default_factory=list)
    source_traceability: list[dict[str, Any]] = Field(min_length=1)
    analysis_traceability: list[dict[str, Any]] = Field(min_length=1)
    claim_traceability: list[TraceabilityEntry] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    human_evidence_decision: HumanEvidenceDecision | None = None
    accepted_evidence_gaps: list[AcceptedEvidenceGap] = Field(default_factory=list)
    quality_review: DeliberationQualityReviewOutput | None = None

    @model_validator(mode="after")
    def validate_viewpoints(self) -> "DeliberationResult":
        ids = [item.viewpoint_id for item in self.analysis_perspectives]
        if len(set(ids)) != len(ids):
            raise ValueError("analysis_perspectives must have unique viewpoint_id values")
        return self
