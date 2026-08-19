from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conclusion.schemas.decision_context import DecisionContext
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult
from conclusion.schemas.position_candidate import PositionCandidate
from conclusion.schemas.strict_references import (
    bind_strict_reference_fields,
    candidate_reference_values,
    decision_context_reference_values,
    unique_strings,
)


class ExcludedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CandidateComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class RecommendedOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    recommendation_type: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class IntegratedOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integrated_option_id: str = Field(min_length=1)
    candidate_ids: list[str] = Field(
        min_length=1,
        description=(
            "One or more exact position_candidate_id values from the input candidates only. "
            "A single-candidate or conditional selection uses one ID. "
            "Never include labels, field names, rationale, or prose."
        ),
    )
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    implementation_direction: str = Field(min_length=1)
    non_combinable_elements: list[str] = Field(default_factory=list)


class ValueConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class MajorTradeoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tradeoff_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    related_claim_ids: list[str] = Field(
        default_factory=list,
        description="Copy exact IDs only from decision_context.key_claim_ids.",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Copy exact IDs only from decision_context.evidence_ids.",
    )

    @model_validator(mode="before")
    @classmethod
    def accept_summary_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "summary" not in value:
            return value
        normalized = dict(value)
        normalized["description"] = normalized.pop("summary")
        return normalized


class HumanDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1)
    question: str = Field(min_length=1)


class DecisionIntegrationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(pattern=r"^conclusion\.decision_integrator$")
    decision_context: DecisionContext
    position_candidates: list[PositionCandidate] = Field(min_length=2, max_length=5)
    decision_evaluation: DecisionEvaluationResult
    requested_integration_candidate_ids: list[str] = Field(default_factory=list)
    user_instruction: str | None = None
    revision_context: dict[str, Any] | None = None


class DecisionIntegrationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_integration_result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    decision_evaluation_result_id: str = Field(min_length=1)
    viable_candidates: list[str] = Field(
        min_length=1,
        description="Exact position_candidate_id values from the input candidates only.",
    )
    excluded_candidates: list[ExcludedCandidate] = Field(default_factory=list)
    candidate_comparison_summary: list[CandidateComparisonSummary] = Field(min_length=1)
    recommended_options: list[RecommendedOption] = Field(min_length=1)
    integrated_option: IntegratedOption | None = None
    unresolved_value_conflicts: list[ValueConflict] = Field(default_factory=list)
    non_negotiable_constraints: list[str] = Field(default_factory=list)
    major_tradeoffs: list[MajorTradeoff] = Field(default_factory=list)
    accepted_uncertainties: list[str] = Field(default_factory=list)
    human_decisions_required: list[HumanDecision] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        refs = decision_context_reference_values(input_data)
        candidate_ids = candidate_reference_values(input_data)
        evaluation = input_data.get("decision_evaluation")
        evaluation_id = (
            evaluation.get("decision_evaluation_result_id")
            if isinstance(evaluation, dict)
            else None
        )
        return bind_strict_reference_fields(
            schema,
            list_fields={
                "viable_candidates": candidate_ids,
                "candidate_ids": candidate_ids,
                "related_claim_ids": refs.get("claim", []),
                "evidence_ids": refs.get("evidence", []),
            },
            scalar_fields={
                "candidate_id": candidate_ids,
                "task_id": unique_strings([input_data.get("task_id")]),
                "decision_evaluation_result_id": unique_strings([evaluation_id]),
            },
        )

    @model_validator(mode="after")
    def viable_and_excluded_are_disjoint(self) -> "DecisionIntegrationResult":
        excluded = {item.candidate_id for item in self.excluded_candidates}
        overlap = set(self.viable_candidates) & excluded
        if overlap:
            raise ValueError(f"candidates cannot be both viable and excluded: {sorted(overlap)}")
        return self
