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


class ComparisonRatings(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    PROBLEM_RELEVANCE: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    EXPECTED_EFFECTIVENESS: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    IMPLEMENTATION_FEASIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    COST_AND_RESOURCE_REQUIREMENTS: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    TIME_TO_IMPACT: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    STAKEHOLDER_IMPACT: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    DISTRIBUTIONAL_EQUITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    ETHICAL_IMPACT: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    LEGAL_FEASIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    POLITICAL_FEASIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    SCALABILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    REVERSIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    RISK: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    EVIDENCE_SUPPORT: EvaluationRating = EvaluationRating.NOT_EVALUABLE


class CandidateComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    ratings: ComparisonRatings


class ConditionalAdvantage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1)
    advantaged_candidate_id: str = Field(min_length=1)


class DisqualificationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SensitivityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1)
    preferred_candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EvaluationInformationGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    criterion: EvaluationCriterion
    status: EvaluationRating


class EvaluationRevisionRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    required_revision: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DecisionEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_evaluation_result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    decision_context_id: str = Field(min_length=1)
    evaluation_framework: EvaluationFramework
    candidate_evaluations: list[CandidateCriterionEvaluation] = Field(min_length=1)
    comparison_matrix: list[CandidateComparison] = Field(min_length=1)
    conditional_advantages: list[ConditionalAdvantage] = Field(default_factory=list)
    disqualification_findings: list[DisqualificationFinding] = Field(default_factory=list)
    sensitivity_analysis: list[SensitivityResult] = Field(min_length=1)
    missing_information: list[EvaluationInformationGap] = Field(default_factory=list)
    revision_recommendations: list[EvaluationRevisionRecommendation] = Field(
        default_factory=list
    )
    status: str = Field(min_length=1)

    @model_validator(mode="after")
    def unique_candidate_criterion_pairs(self) -> "DecisionEvaluationResult":
        pairs = [(item.candidate_id, item.criterion) for item in self.candidate_evaluations]
        if len(pairs) != len(set(pairs)):
            raise ValueError("candidate/criterion evaluation pairs must be unique")
        return self
