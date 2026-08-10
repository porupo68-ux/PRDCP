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


class VisualPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visual_plan_id: str = Field(min_length=1)
    citation_validated_script_id: str = Field(min_length=1)
    visual_cues: list[VisualCue] = Field(default_factory=list)
    chart_requests: list[ChartRequest] = Field(default_factory=list)
    asset_requirements: list[AssetRequirement] = Field(default_factory=list)
    source_display_rules: list[dict[str, Any]] = Field(default_factory=list)
    visual_integrity_warnings: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ids(self) -> "VisualPlan":
        for values, label in (
            ([item.visual_cue_id for item in self.visual_cues], "visual_cue_id"),
            ([item.chart_request_id for item in self.chart_requests], "chart_request_id"),
            ([item.asset_requirement_id for item in self.asset_requirements], "asset_requirement_id"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} values must be unique")
        return self


class VisualDirectionTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_agent_id: str = Field(default="playwright.visual_director")
    production_context: ProductionContext
    citation_validated_script: CitationValidatedScript
    citation_manifest: CitationManifest
    revision_context: dict[str, Any] | None = None

