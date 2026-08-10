from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conclusion.schemas.decision_context import DecisionContext
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult
from conclusion.schemas.position_candidate import PositionCandidate


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
    viable_candidates: list[str] = Field(min_length=1)
    excluded_candidates: list[dict[str, Any]] = Field(default_factory=list)
    candidate_comparison_summary: list[dict[str, Any]] = Field(min_length=1)
    recommended_options: list[dict[str, Any]] = Field(min_length=1)
    integrated_option: dict[str, Any] | None = None
    unresolved_value_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    non_negotiable_constraints: list[str] = Field(default_factory=list)
    major_tradeoffs: list[dict[str, Any]] = Field(default_factory=list)
    accepted_uncertainties: list[str] = Field(default_factory=list)
    human_decisions_required: list[dict[str, Any]] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def viable_and_excluded_are_disjoint(self) -> "DecisionIntegrationResult":
        excluded = {str(item.get("candidate_id")) for item in self.excluded_candidates}
        overlap = set(self.viable_candidates) & excluded
        if overlap:
            raise ValueError(f"candidates cannot be both viable and excluded: {sorted(overlap)}")
        return self
