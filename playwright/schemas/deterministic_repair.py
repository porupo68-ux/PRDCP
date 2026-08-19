from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class PlaywrightRepairDisposition(str, Enum):
    DETERMINISTIC_REPAIRABLE = "DETERMINISTIC_REPAIRABLE"
    AGENT_REVISION_REQUIRED = "AGENT_REVISION_REQUIRED"
    UPSTREAM_REVISION_REQUIRED = "UPSTREAM_REVISION_REQUIRED"
    NON_REPAIRABLE = "NON_REPAIRABLE"


class PlaywrightDeterministicRepairType(str, Enum):
    CITATION_MAPPING_RECONSTRUCTION = "CITATION_MAPPING_RECONSTRUCTION"


class PlaywrightDeterministicRepairRecord(BaseModel):
    """Audit artifact for a zero-Provider, content-preserving local repair."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    repair_id: str = Field(min_length=1)
    repair_type: PlaywrightDeterministicRepairType
    finding_ids: list[str] = Field(min_length=1)
    paragraph_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    mapping_ids_added: list[str] = Field(min_length=1)
    donor_mapping_ids: list[str] = Field(min_length=1)
    citation_manifest_hash_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    citation_manifest_hash_after: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    final_conclusion_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    production_context_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    narrative_blueprint_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    script_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    citation_validated_script_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    visual_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider_calls: int = Field(default=0, ge=0, le=0)
    retrieval_calls: int = Field(default=0, ge=0, le=0)
    created_at: datetime
