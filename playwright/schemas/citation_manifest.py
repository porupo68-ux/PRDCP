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
    # Script Draft is the canonical owner of the claim set.  This field is
    # persisted explicitly so downstream validation and Delivery never have
    # to infer claim coverage from issue lists or a subset of mappings.
    # The default keeps pre-Cycle-047 artifacts readable; the Manager binds
    # the canonical value before a newly generated Manifest is persisted.
    supported_claim_ids: list[str] = Field(default_factory=list)
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
        if len(self.supported_claim_ids) != len(set(self.supported_claim_ids)):
            raise ValueError("supported_claim_ids values must be unique")
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

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(default="playwright.evidence_citation_editor")
    production_context: ProductionContext
    script_draft: ScriptDraft
    revision_context: dict[str, Any] | None = None


class CitationEditingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_validated_script: CitationValidatedScript
    citation_manifest: CitationManifest

    @model_validator(mode="after")
    def require_linked_artifacts(self) -> "CitationEditingResult":
        if (
            self.citation_validated_script.source_script_draft_id
            != self.citation_manifest.script_draft_id
        ):
            raise ValueError(
                "Citation artifacts must reference the same Script Draft"
            )
        if (
            self.citation_validated_script.citation_manifest_id
            != self.citation_manifest.citation_manifest_id
        ):
            raise ValueError(
                "Citation Validated Script must reference its returned Citation Manifest"
            )
        return self

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        from playwright.schemas.strict_references import (
            bind_array_item_variants,
            bind_strict_reference_fields,
            unique_strings,
        )

        context = input_data.get("production_context") or {}
        draft = input_data.get("script_draft") or {}
        source_manifest = context.get("source_manifest") or []
        evidence_to_source = {
            item.get("evidence_id"): item.get("source_id")
            for item in source_manifest
            if isinstance(item, dict)
            and isinstance(item.get("evidence_id"), str)
            and isinstance(item.get("source_id"), str)
        }
        sections = draft.get("sections") or []
        paragraphs = [
            (section, paragraph)
            for section in sections
            if isinstance(section, dict)
            for paragraph in section.get("paragraphs") or []
            if isinstance(paragraph, dict)
        ]
        section_ids = unique_strings(
            [section.get("section_id") for section in sections if isinstance(section, dict)]
        )
        paragraph_ids = unique_strings(
            [paragraph.get("paragraph_id") for _section, paragraph in paragraphs]
        )
        claim_ids = unique_strings(list(context.get("must_include_claim_ids") or []))
        evidence_ids = unique_strings(
            list(context.get("must_include_evidence_ids") or [])
        )
        source_ids = unique_strings(list(evidence_to_source.values()))
        script_draft_id = str(draft.get("script_draft_id") or "")

        bind_strict_reference_fields(
            schema,
            list_fields={
                "claim_ids": claim_ids,
                "supported_claim_ids": claim_ids,
                "evidence_ids": evidence_ids,
                "source_ids": source_ids,
            },
            scalar_fields={
                "source_script_draft_id": [script_draft_id],
                "script_draft_id": [script_draft_id],
                "section_id": section_ids,
                "paragraph_id": paragraph_ids,
                "evidence_id": evidence_ids,
                "source_id": source_ids,
            },
        )
        # The enum prevents unknown IDs; exact bounds plus Pydantic's
        # uniqueness validator prevent a partial known-ID claim set from being
        # accepted as a complete Citation Manifest.
        supported_targets: list[dict[str, Any]] = []

        def find_supported_claims(node: Any) -> None:
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict):
                    target = properties.get("supported_claim_ids")
                    if isinstance(target, dict):
                        supported_targets.append(target)
                for child in node.values():
                    find_supported_claims(child)
            elif isinstance(node, list):
                for child in node:
                    find_supported_claims(child)

        find_supported_claims(schema)
        if len(supported_targets) != 1:
            raise ValueError(
                "strict schema expected one supported_claim_ids field, found "
                f"{len(supported_targets)}"
            )
        supported_targets[0]["minItems"] = len(claim_ids)
        supported_targets[0]["maxItems"] = len(claim_ids)
        mapping_variants = []
        for _section, paragraph in paragraphs:
            paragraph_id = str(paragraph.get("paragraph_id") or "")
            paragraph_evidence = unique_strings(list(paragraph.get("evidence_ids") or []))
            paragraph_sources = unique_strings(
                [evidence_to_source.get(value) for value in paragraph_evidence]
            )
            mapping_variants.append(
                {
                    "scalar_fields": {"paragraph_id": [paragraph_id]},
                    "list_fields": {
                        "claim_ids": unique_strings(list(paragraph.get("claim_ids") or [])),
                        "evidence_ids": paragraph_evidence,
                        "source_ids": paragraph_sources,
                    },
                }
            )
        return bind_array_item_variants(
            schema,
            array_field="mappings",
            variants=mapping_variants,
        )
