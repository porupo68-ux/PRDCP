from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DecisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_context_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    deliberation_result_id: str = Field(min_length=1)
    decision_question: str = Field(min_length=1)
    target_problem: dict[str, Any]
    goals: list[str] = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    non_negotiable_constraints: list[str] = Field(default_factory=list)
    affected_stakeholders: list[dict[str, Any]] = Field(default_factory=list)
    major_viewpoints: list[dict[str, Any]] = Field(min_length=1, max_length=3)
    key_claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    analysis_ids: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    tradeoffs: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evaluation_criteria: list[str] = Field(min_length=1)
    value_profiles: list[dict[str, Any]] = Field(min_length=1)
