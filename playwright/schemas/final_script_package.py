from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from playwright.schemas.citation_manifest import CitationManifest, CitationValidatedScript
from playwright.schemas.visual_plan import VisualPlan


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FinalScriptPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    final_script_package_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    final_conclusion_id: str = Field(min_length=1)
    human_selection_id: str = Field(min_length=1)
    title_candidates: list[str] = Field(min_length=1, max_length=5)
    thumbnail_text_candidates: list[str] = Field(min_length=1, max_length=5)
    script: CitationValidatedScript
    citation_manifest: CitationManifest
    visual_plan: VisualPlan
    production_summary: dict[str, Any]
    limitations_to_disclose: list[str] = Field(default_factory=list)
    unresolved_production_items: list[dict[str, Any]] = Field(default_factory=list)
    traceability_manifest: dict[str, Any]
    final_gate_result: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)

