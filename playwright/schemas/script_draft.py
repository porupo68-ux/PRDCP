from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from playwright.schemas.narrative_blueprint import NarrativeBlueprint
from playwright.schemas.production_context import ProductionContext


class ScriptParagraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraph_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    speaker_text: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    citation_required: bool
    uncertainty_disclosed: bool = False
    limitation_disclosed: bool = False
    rhetorical_function: str = Field(min_length=1)


class ScriptSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    section_type: str = Field(min_length=1)
    heading: str = Field(min_length=1)
    target_duration_seconds: int = Field(ge=1)
    paragraphs: list[ScriptParagraph] = Field(min_length=1)
    transition_text: str | None = None

    @model_validator(mode="after")
    def validate_paragraph_order(self) -> "ScriptSection":
        sequences = [item.sequence for item in self.paragraphs]
        if sequences != list(range(1, len(self.paragraphs) + 1)):
            raise ValueError("Paragraph sequence must be contiguous from 1 within each section")
        return self


class ScriptDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_draft_id: str = Field(min_length=1)
    narrative_blueprint_id: str = Field(min_length=1)
    title_candidates: list[str] = Field(min_length=1, max_length=5)
    thumbnail_text_candidates: list[str] = Field(min_length=1, max_length=5)
    estimated_duration_seconds: int = Field(ge=60, le=7200)
    estimated_character_count: int = Field(ge=1)
    sections: list[ScriptSection] = Field(min_length=1)
    disclosure_summary: list[str] = Field(default_factory=list)
    unresolved_items: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_section_and_paragraph_ids(self) -> "ScriptDraft":
        section_ids = [item.section_id for item in self.sections]
        if len(section_ids) != len(set(section_ids)):
            raise ValueError("Script section_id values must be unique")
        if [item.sequence for item in self.sections] != list(range(1, len(self.sections) + 1)):
            raise ValueError("Script section sequence must be contiguous from 1")
        paragraph_ids = [p.paragraph_id for section in self.sections for p in section.paragraphs]
        if len(paragraph_ids) != len(set(paragraph_ids)):
            raise ValueError("Script paragraph_id values must be unique")
        return self


class ScriptWritingTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent_id: str = Field(default="playwright.scriptwriter")
    production_context: ProductionContext
    narrative_blueprint: NarrativeBlueprint
    revision_context: dict[str, Any] | None = None

