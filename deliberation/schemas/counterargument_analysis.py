from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Challenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: str = Field(min_length=1)
    target_claim_ids: list[str] = Field(min_length=1)
    argument: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    strength: str = Field(min_length=1)


class IntegrationRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=1)
    target_item_id: str = Field(min_length=1)
    required_change: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_counterargument_ids: list[str] = Field(min_length=1)


class CounterargumentAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    steelman_arguments: list[Challenge] = Field(min_length=1)
    counterarguments: list[Challenge] = Field(min_length=1)
    contrary_evidence: list[dict[str, Any]] = Field(default_factory=list)
    exception_conditions: list[dict[str, Any]] = Field(default_factory=list)
    falsification_conditions: list[dict[str, Any]] = Field(default_factory=list)
    alternative_interpretations: list[dict[str, Any]] = Field(default_factory=list)
    overlooked_stakeholders: list[dict[str, Any]] = Field(default_factory=list)
    false_balance_risks: list[dict[str, Any]] = Field(default_factory=list)
    required_revisions: list[IntegrationRevision] = Field(min_length=1)
    remaining_uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_challenge_ids(self) -> "CounterargumentAnalysisResult":
        challenges = self.steelman_arguments + self.counterarguments
        ids = [item.challenge_id for item in challenges]
        if len(set(ids)) != len(ids):
            raise ValueError("challenge IDs must be unique")
        return self
