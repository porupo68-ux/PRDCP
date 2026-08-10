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


class CitationMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    citation_mapping_id: str = Field(min_length=1)
    paragraph_id: str = Field(min_length=1)
    claim_text: str = Field(min_length=1)
    claim_type: ScriptClaimType
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    citation_locator: dict[str, Any] | None = None
    support_status: str = Field(min_length=1)
    wording_risk: str = Field(min_length=1)
    required_revision: str | None = None


class CitationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_manifest_id: str = Field(min_length=1)
    script_draft_id: str = Field(min_length=1)
    mappings: list[CitationMapping] = Field(default_factory=list)
    unsupported_claims: list[dict[str, Any]] = Field(default_factory=list)
    partially_supported_claims: list[dict[str, Any]] = Field(default_factory=list)
    missing_locators: list[dict[str, Any]] = Field(default_factory=list)
    source_list: list[dict[str, Any]] = Field(default_factory=list)
    disclosure_checks: list[dict[str, Any]] = Field(default_factory=list)
    revision_summary: list[dict[str, Any]] = Field(default_factory=list)

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
    paragraph_revision_map: list[dict[str, Any]] = Field(default_factory=list)
    citation_manifest_id: str = Field(min_length=1)
    unresolved_citation_issues: list[dict[str, Any]] = Field(default_factory=list)
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

