from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from playwright.schemas.production_context import ProductionContext


class NarrativeSectionType(str, Enum):
    HOOK = "HOOK"
    QUESTION = "QUESTION"
    CONTEXT = "CONTEXT"
    GENERAL_OPINION = "GENERAL_OPINION"
    EVIDENCE = "EVIDENCE"
    ANALYSIS = "ANALYSIS"
    COUNTERPOINT = "COUNTERPOINT"
    DECISION = "DECISION"
    LIMITATION = "LIMITATION"
    CONCLUSION = "CONCLUSION"
    CTA = "CTA"


class NarrativeSection(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    section_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    section_type: NarrativeSectionType
    purpose: str = Field(min_length=1)
    key_message: str = Field(min_length=1)
    target_duration_seconds: int = Field(ge=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    required_counterpoints: list[str] = Field(default_factory=list)
    required_limitations: list[str] = Field(default_factory=list)
    transition_goal: str | None = None


class NarrativePlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1)
    items: list[str] = Field(default_factory=list)


class NarrativeBlueprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    narrative_blueprint_id: str = Field(min_length=1)
    production_context_id: str = Field(min_length=1)
    narrative_strategy: str = Field(min_length=1)
    central_question: str = Field(min_length=1)
    central_message: str = Field(min_length=1)
    estimated_duration_seconds: int = Field(ge=60, le=7200)
    sections: list[NarrativeSection] = Field(min_length=1)
    must_include_claim_ids: list[str] = Field(min_length=1)
    must_include_evidence_ids: list[str] = Field(min_length=1)
    uncertainty_placement: list[NarrativePlacement] = Field(default_factory=list)
    limitation_placement: list[NarrativePlacement] = Field(default_factory=list)
    emotional_arc: list[str] = Field(default_factory=list)
    pacing_notes: list[str] = Field(default_factory=list)
    prohibited_reframings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_identity_and_order(self) -> "NarrativeBlueprint":
        ids = [item.section_id for item in self.sections]
        if len(ids) != len(set(ids)):
            raise ValueError("Narrative section_id values must be unique")
        if [item.sequence for item in self.sections] != list(range(1, len(self.sections) + 1)):
            raise ValueError("Narrative section sequence must be contiguous from 1")
        return self


class NarrativeDesignTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(default="playwright.narrative_architect")
    production_context: ProductionContext
    revision_context: dict[str, Any] | None = None
