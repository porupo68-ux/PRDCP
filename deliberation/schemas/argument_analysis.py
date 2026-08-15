from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deliberation.schemas.identifiers import (
    ARGUMENT_ANALYSIS_PREFIX,
    canonicalize_analysis_id,
)


class ClaimSupportStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    CONTESTED = "CONTESTED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    claim_type: str = Field(min_length=1)
    importance: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    support_status: ClaimSupportStatus


class Premise(BaseModel):
    model_config = ConfigDict(extra="forbid")

    premise_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class Warrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warrant_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)


class LogicalGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gap_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    affected_claim_ids: list[str] = Field(min_length=1)
    severity: str = Field(min_length=1)


class ClaimEvidenceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    relationship: str = Field(min_length=1)


class ArgumentAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    analysis_id: str = Field(
        min_length=1,
        description="Unique argument analysis identifier using argument_analysis_*",
    )
    task_id: str = Field(min_length=1)
    central_claims: list[Claim] = Field(min_length=1)
    premises: list[Premise] = Field(default_factory=list)
    warrants: list[Warrant] = Field(default_factory=list)
    logical_gaps: list[LogicalGap] = Field(default_factory=list)
    descriptive_claim_ids: list[str] = Field(default_factory=list)
    normative_claim_ids: list[str] = Field(default_factory=list)
    evidence_mappings: list[ClaimEvidenceMapping] = Field(min_length=1)
    exception_conditions: list[str] = Field(default_factory=list)
    scope_conditions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("analysis_id", mode="before")
    @classmethod
    def normalize_analysis_id(cls, value: str) -> str:
        return canonicalize_analysis_id(
            value,
            canonical_prefix=ARGUMENT_ANALYSIS_PREFIX,
            legacy_prefixes=("arg_analysis_", "analysis_argument_"),
        )

    @model_validator(mode="after")
    def validate_claim_graph(self) -> "ArgumentAnalysisResult":
        claim_ids = [claim.claim_id for claim in self.central_claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("central_claims must have unique claim_id values")
        known = set(claim_ids)
        if set(self.descriptive_claim_ids + self.normative_claim_ids) - known:
            raise ValueError("claim classification references an unknown claim_id")
        mapped = {mapping.claim_id for mapping in self.evidence_mappings}
        if mapped - known:
            raise ValueError("evidence_mappings reference an unknown claim_id")
        if known - mapped:
            raise ValueError("every central claim requires an evidence mapping")
        return self
