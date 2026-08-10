from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConclusionPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conclusion_package_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    general_opinion: str = Field(min_length=1)
    decision_question: str = Field(min_length=1)
    problem_summary: str = Field(min_length=1)
    deliberation_summary: str = Field(min_length=1)
    options: list[dict[str, Any]] = Field(min_length=2, max_length=5)
    comparison_matrix: list[dict[str, Any]] = Field(min_length=1)
    primary_recommendation: dict[str, Any] | None = None
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    integrated_option: dict[str, Any] | None = None
    key_tradeoffs: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_value_conflicts: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    evidence_traceability: list[dict[str, Any]] = Field(min_length=1)
    analysis_traceability: list[dict[str, Any]] = Field(min_length=1)
    selection_required: bool = True
    quality_review: dict[str, Any] | None = None
