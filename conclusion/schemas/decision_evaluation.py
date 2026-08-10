from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conclusion.schemas.decision_context import DecisionContext
from conclusion.schemas.evaluation_framework import (
    EvaluationCriterion,
    EvaluationFramework,
    EvaluationRating,
)
from conclusion.schemas.position_candidate import PositionCandidate


class DecisionEvaluationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(pattern=r"^conclusion\.decision_evaluator$")
    decision_context: DecisionContext
    position_candidates: list[PositionCandidate] = Field(min_length=2, max_length=5)
    evaluation_framework: EvaluationFramework
    revision_context: dict[str, Any] | None = None


class CandidateCriterionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    candidate_id: str = Field(min_length=1)
    criterion: EvaluationCriterion
    rating: EvaluationRating
    rationale: str = Field(min_length=1)
    supporting_claim_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    supporting_analysis_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    blocking_issue: bool = False
    blocking_reason: str | None = None

    @model_validator(mode="after")
    def blocking_reason_required(self) -> "CandidateCriterionEvaluation":
        if self.blocking_issue and not self.blocking_reason:
            raise ValueError("blocking_reason is required for a blocking issue")
        return self


class DecisionEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_evaluation_result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    decision_context_id: str = Field(min_length=1)
    evaluation_framework: EvaluationFramework
    candidate_evaluations: list[CandidateCriterionEvaluation] = Field(min_length=1)
    comparison_matrix: list[dict[str, Any]] = Field(min_length=1)
    conditional_advantages: list[dict[str, Any]] = Field(default_factory=list)
    disqualification_findings: list[dict[str, Any]] = Field(default_factory=list)
    sensitivity_analysis: list[dict[str, Any]] = Field(min_length=1)
    missing_information: list[dict[str, Any]] = Field(default_factory=list)
    revision_recommendations: list[dict[str, Any]] = Field(default_factory=list)
    status: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_candidate_criterion_pairs(self) -> "DecisionEvaluationResult":
        pairs = [(item.candidate_id, item.criterion) for item in self.candidate_evaluations]
        if len(pairs) != len(set(pairs)):
            raise ValueError("candidate/criterion evaluation pairs must be unique")
        return self
