from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from playwright.schemas.production_context import ProductionContext
from playwright.schemas.script_draft import ScriptDraft, ScriptSection


class ScriptClaimType(str, Enum):
    SUPPORTED_FACT = "SUPPORTED_FACT"
    PARTIALLY_SUPPORTED = "PARTIALLY_SUPPORTED"
    INTERPRETATION = "INTERPRETATION"
    NORMATIVE_JUDGMENT = "NORMATIVE_JUDGMENT"
    RHETORICAL_EXPRESSION = "RHETORICAL_EXPRESSION"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


class CitationLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ids: list[str] = Field(default_factory=list)


class CitationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraph_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class CitationSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    research_question_ids: list[str] = Field(default_factory=list)


class DisclosureCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limitation: str = Field(min_length=1)
    preserved: bool


class ParagraphRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paragraph_id: str = Field(min_length=1)
    before_text: str | None = None
    after_text: str | None = None
    reason: str = Field(min_length=1)


class CitationMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    citation_mapping_id: str = Field(min_length=1)
    paragraph_id: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)
    claim_type: ScriptClaimType
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    citation_locator: CitationLocator | None = None
    support_status: str = Field(min_length=1)
    wording_risk: str = Field(min_length=1)
    required_revision: str | None = None


class CitationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_manifest_id: str = Field(min_length=1)
    script_draft_id: str = Field(min_length=1)
    mappings: list[CitationMapping] = Field(default_factory=list)
    unsupported_claims: list[CitationIssue] = Field(default_factory=list)
    partially_supported_claims: list[CitationIssue] = Field(default_factory=list)
    missing_locators: list[CitationIssue] = Field(default_factory=list)
    source_list: list[CitationSource] = Field(default_factory=list)
    disclosure_checks: list[DisclosureCheck] = Field(default_factory=list)
    revision_summary: list[ParagraphRevision] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_mapping_ids(self) -> "CitationManifest":
        ids = [item.citation_mapping_id for item in self.mappings]
        if len(ids) != len(set(ids)):
            raise ValueError("citation_mapping_id values must be unique")
        return self


class CitationValidatedScript(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_validated_script_id: str = Field(min_length=1)
    source_script_draft_id: str = Field(min_length=1)
    sections: list[ScriptSection] = Field(min_length=1)
    paragraph_revision_map: list[ParagraphRevision] = Field(default_factory=list)
    citation_manifest_id: str = Field(min_length=1)
    unresolved_citation_issues: list[CitationIssue] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CitationEditingTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent_id: str = Field(default="playwright.evidence_citation_editor")
    production_context: ProductionContext
    script_draft: ScriptDraft
    revision_context: dict[str, Any] | None = None


class CitationEditingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_validated_script: CitationValidatedScript
    citation_manifest: CitationManifest
