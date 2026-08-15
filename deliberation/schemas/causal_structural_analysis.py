from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deliberation.schemas.identifiers import (
    CAUSAL_ANALYSIS_PREFIX,
    canonicalize_analysis_id,
)


class CausalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    status: str = Field(min_length=1)


class CausationRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_description: str = Field(min_length=1)
    evidence_linked: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "risk_description" in value:
            return value
        return {
            "risk_description": value.get("description"),
            "evidence_linked": value.get("evidence_ids", []),
        }


class CausalCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(min_length=1)
    evidence_linked: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_evidence_ids_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "evidence_ids" not in value:
            return value
        normalized = dict(value)
        normalized["evidence_linked"] = normalized.pop("evidence_ids")
        return normalized


class CausalEvidenceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    mapped_item_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "evidence_id" in value:
            return value
        evidence_ids = value.get("evidence_ids") or []
        return {
            "evidence_id": evidence_ids[0] if evidence_ids else "unmapped_evidence",
            "mapped_item_ids": [value.get("item_id", "unmapped_item")],
        }


class CausalStructuralAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(
        min_length=1,
        description="Unique causal analysis identifier using causal_analysis_*",
    )
    task_id: str = Field(min_length=1)
    causal_claims: list[CausalItem] = Field(min_length=1)
    mechanisms: list[CausalItem] = Field(min_length=1)
    structural_factors: list[CausalItem] = Field(min_length=1)
    feedback_loops: list[CausalItem] = Field(default_factory=list)
    alternative_explanations: list[CausalItem] = Field(default_factory=list)
    correlation_causation_risks: list[CausationRisk] = Field(default_factory=list)
    necessary_conditions: list[CausalCondition] = Field(default_factory=list)
    sufficient_conditions: list[CausalCondition] = Field(default_factory=list)
    evidence_mappings: list[CausalEvidenceMapping] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("analysis_id", mode="before")
    @classmethod
    def normalize_analysis_id(cls, value: str) -> str:
        return canonicalize_analysis_id(
            value,
            canonical_prefix=CAUSAL_ANALYSIS_PREFIX,
            legacy_prefixes=("analysis_causal_",),
        )

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
