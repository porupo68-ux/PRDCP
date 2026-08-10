from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Stakeholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stakeholder_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class StakeholderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    stakeholder_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ExistingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(min_length=1)
    actor_stakeholder_ids: list[str] = Field(min_length=1)
    description: str = Field(min_length=1)
    implementation_status: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class StakeholderResponseAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    stakeholders: list[Stakeholder] = Field(min_length=1)
    interests: list[StakeholderItem] = Field(default_factory=list)
    authority_and_capacity: list[StakeholderItem] = Field(min_length=1)
    existing_responses: list[ExistingResponse] = Field(default_factory=list)
    response_effectiveness: list[dict[str, Any]] = Field(default_factory=list)
    incentives: list[dict[str, Any]] = Field(default_factory=list)
    implementation_barriers: list[dict[str, Any]] = Field(default_factory=list)
    distributional_effects: list[dict[str, Any]] = Field(default_factory=list)
    evidence_mappings: list[dict[str, Any]] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_stakeholders(self) -> "StakeholderResponseAnalysisResult":
        ids = [item.stakeholder_id for item in self.stakeholders]
        if len(set(ids)) != len(ids):
            raise ValueError("stakeholders must have unique stakeholder_id values")
        known = set(ids)
        if any(item.stakeholder_id not in known for item in self.interests):
            raise ValueError("interests reference an unknown stakeholder_id")
        if any(item.stakeholder_id not in known for item in self.authority_and_capacity):
            raise ValueError("authority_and_capacity references an unknown stakeholder_id")
        return self
