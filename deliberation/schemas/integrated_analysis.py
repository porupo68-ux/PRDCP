from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Viewpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    viewpoint_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    position: str = Field(min_length=1)
    supporting_claim_ids: list[str] = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(min_length=1)
    counterarguments: list[str] = Field(default_factory=list)
    strongest_objections: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    scope_conditions: list[str] = Field(default_factory=list)


class InitialIntegratedAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_id: str = Field(min_length=1)
    problem_definition: dict[str, Any]
    key_claims: list[dict[str, Any]] = Field(min_length=1)
    causal_structure: dict[str, Any]
    stakeholder_structure: dict[str, Any]
    existing_response_assessment: list[dict[str, Any]] = Field(default_factory=list)
    agreements: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_items: list[dict[str, Any]] = Field(default_factory=list)
    candidate_viewpoints: list[Viewpoint] = Field(min_length=1, max_length=3)
    traceability_index: dict[str, list[str]] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_claims(self) -> "InitialIntegratedAnalysis":
        claim_ids: list[str] = []
        for claim in self.key_claims:
            claim_id = claim.get("claim_id")
            evidence_ids = claim.get("evidence_ids")
            if not isinstance(claim_id, str) or not claim_id:
                raise ValueError("every integrated key claim requires claim_id")
            if not isinstance(evidence_ids, list) or not evidence_ids:
                raise ValueError("every integrated key claim requires evidence_ids")
            claim_ids.append(claim_id)
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("integrated key claims must have unique claim_id values")
        return self


class IntegrationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str = Field(min_length=1)
    target_item_id: str = Field(min_length=1)
    change_type: str = Field(min_length=1)
    before_summary: str = Field(min_length=1)
    after_summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_counterargument_ids: list[str] = Field(min_length=1)


class FinalIntegratedAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_id: str = Field(min_length=1)
    previous_integration_id: str = Field(min_length=1)
    problem_definition: dict[str, Any]
    key_claims: list[dict[str, Any]] = Field(min_length=1)
    causal_structure: dict[str, Any]
    stakeholder_structure: dict[str, Any]
    existing_response_assessment: list[dict[str, Any]] = Field(default_factory=list)
    major_viewpoints: list[Viewpoint] = Field(min_length=1, max_length=3)
    agreements: list[dict[str, Any]] = Field(default_factory=list)
    disagreements: list[dict[str, Any]] = Field(default_factory=list)
    tradeoffs: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_questions: list[dict[str, Any]] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    integration_changes: list[IntegrationChange] = Field(min_length=1)
    traceability_index: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_lineage(self) -> "FinalIntegratedAnalysis":
        if self.integration_id == self.previous_integration_id:
            raise ValueError("final integration must have a new integration_id")
        ids = [item.viewpoint_id for item in self.major_viewpoints]
        if len(set(ids)) != len(ids):
            raise ValueError("major_viewpoints must have unique viewpoint_id values")
        claim_ids = [claim.get("claim_id") for claim in self.key_claims]
        if any(not isinstance(claim_id, str) or not claim_id for claim_id in claim_ids):
            raise ValueError("final key claims require claim_id")
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("final key claims must have unique claim_id values")
        return self
