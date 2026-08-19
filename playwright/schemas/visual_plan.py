from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from playwright.schemas.citation_manifest import CitationManifest, CitationValidatedScript
from playwright.schemas.production_context import ProductionContext


class VisualType(str, Enum):
    BROLL = "BROLL"
    CHART = "CHART"
    GRAPH = "GRAPH"
    MAP = "MAP"
    TIMELINE = "TIMELINE"
    QUOTE_CARD = "QUOTE_CARD"
    SCREENSHOT = "SCREENSHOT"
    TEXT_OVERLAY = "TEXT_OVERLAY"
    ANIMATION = "ANIMATION"
    DIAGRAM = "DIAGRAM"
    ICON = "ICON"
    NONE = "NONE"


class AssetRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_requirement_id: str = Field(min_length=1)
    asset_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    licensing_requirement: str = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)


class VisualCue(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    visual_cue_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    paragraph_id: str = Field(min_length=1)
    visual_type: VisualType
    description: str = Field(min_length=1)
    target_duration_seconds: int = Field(ge=0)
    on_screen_text: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    asset_requirement_ids: list[str] = Field(default_factory=list)
    factual_visual: bool = False
    citation_display_required: bool = False


class ChartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chart_request_id: str = Field(min_length=1)
    paragraph_id: str = Field(min_length=1)
    chart_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    data_source_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    x_axis: str | None = None
    y_axis: str | None = None
    required_annotations: list[str] = Field(default_factory=list)
    prohibited_transformations: list[str] = Field(default_factory=list)


class SourceDisplayRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: str = Field(min_length=1)


class VisualIntegrityWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warning_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    paragraph_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)


class VisualPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_plan_id: str = Field(min_length=1)
    citation_validated_script_id: str = Field(min_length=1)
    visual_cues: list[VisualCue] = Field(default_factory=list)
    chart_requests: list[ChartRequest] = Field(default_factory=list)
    asset_requirements: list[AssetRequirement] = Field(default_factory=list)
    source_display_rules: list[SourceDisplayRule] = Field(default_factory=list)
    visual_integrity_warnings: list[VisualIntegrityWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ids(self) -> "VisualPlan":
        for values, label in (
            ([item.visual_cue_id for item in self.visual_cues], "visual_cue_id"),
            ([item.chart_request_id for item in self.chart_requests], "chart_request_id"),
            ([item.asset_requirement_id for item in self.asset_requirements], "asset_requirement_id"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} values must be unique")
        asset_ids = {
            item.asset_requirement_id for item in self.asset_requirements
        }
        unknown_asset_ids = sorted(
            {
                asset_id
                for cue in self.visual_cues
                for asset_id in cue.asset_requirement_ids
                if asset_id not in asset_ids
            }
        )
        if unknown_asset_ids:
            raise ValueError(
                "visual_cues reference unknown asset_requirement_ids: "
                + ", ".join(unknown_asset_ids)
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
        script = input_data.get("citation_validated_script") or {}
        manifest = input_data.get("citation_manifest") or {}
        source_manifest = context.get("source_manifest") or []
        source_ids = unique_strings(
            [
                item.get("source_id")
                for item in source_manifest
                if isinstance(item, dict)
            ]
        )
        evidence_ids = unique_strings(
            list(context.get("must_include_evidence_ids") or [])
        )
        source_by_paragraph: dict[str, list[str]] = {}
        for mapping in manifest.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            paragraph_id = mapping.get("paragraph_id")
            if isinstance(paragraph_id, str):
                source_by_paragraph.setdefault(paragraph_id, []).extend(
                    list(mapping.get("source_ids") or [])
                )
        sections = script.get("sections") or []
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
        validated_script_id = str(
            script.get("citation_validated_script_id") or ""
        )
        bind_strict_reference_fields(
            schema,
            list_fields={
                "evidence_ids": evidence_ids,
                "source_ids": source_ids,
                "data_source_ids": source_ids,
            },
            scalar_fields={
                "citation_validated_script_id": [validated_script_id],
                "section_id": section_ids,
                "paragraph_id": paragraph_ids,
            },
        )

        cue_variants = []
        chart_variants = []
        for section, paragraph in paragraphs:
            paragraph_id = str(paragraph.get("paragraph_id") or "")
            paragraph_evidence = unique_strings(list(paragraph.get("evidence_ids") or []))
            paragraph_sources = unique_strings(source_by_paragraph.get(paragraph_id, []))
            scalar_fields = {
                "section_id": [str(section.get("section_id") or "")],
                "paragraph_id": [paragraph_id],
            }
            cue_variants.append(
                {
                    "scalar_fields": scalar_fields,
                    "list_fields": {
                        "evidence_ids": paragraph_evidence,
                        "source_ids": paragraph_sources,
                    },
                }
            )
            chart_variants.append(
                {
                    "scalar_fields": {"paragraph_id": [paragraph_id]},
                    "list_fields": {
                        "evidence_ids": paragraph_evidence,
                        "data_source_ids": paragraph_sources,
                    },
                }
            )
        bind_array_item_variants(
            schema,
            array_field="visual_cues",
            variants=cue_variants,
        )
        return bind_array_item_variants(
            schema,
            array_field="chart_requests",
            variants=chart_variants,
        )


class VisualDirectionTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(default="playwright.visual_director")
    production_context: ProductionContext
    citation_validated_script: CitationValidatedScript
    citation_manifest: CitationManifest
    revision_context: dict[str, Any] | None = None
