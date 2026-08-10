from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CausalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    status: str = Field(min_length=1)


class CausalStructuralAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    causal_claims: list[CausalItem] = Field(min_length=1)
    mechanisms: list[CausalItem] = Field(min_length=1)
    structural_factors: list[CausalItem] = Field(min_length=1)
    feedback_loops: list[CausalItem] = Field(default_factory=list)
    alternative_explanations: list[CausalItem] = Field(default_factory=list)
    correlation_causation_risks: list[dict[str, Any]] = Field(default_factory=list)
    necessary_conditions: list[dict[str, Any]] = Field(default_factory=list)
    sufficient_conditions: list[dict[str, Any]] = Field(default_factory=list)
    evidence_mappings: list[dict[str, Any]] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_items(self) -> "CausalStructuralAnalysisResult":
        items = (
            self.causal_claims
            + self.mechanisms
            + self.structural_factors
            + self.feedback_loops
            + self.alternative_explanations
        )
        ids = [item.item_id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("causal and structural item IDs must be unique")
        return self
