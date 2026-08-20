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
    CITATION_MANIFEST_CONTRACT_RECONSTRUCTION = (
        "CITATION_MANIFEST_CONTRACT_RECONSTRUCTION"
    )


class PlaywrightDeterministicRepairRecord(BaseModel):
    """Audit artifact for a zero-Provider, content-preserving local repair."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    repair_id: str = Field(min_length=1)
    repair_type: PlaywrightDeterministicRepairType
    finding_ids: list[str] = Field(min_length=1)
    paragraph_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    mapping_ids_added: list[str] = Field(default_factory=list)
    donor_mapping_ids: list[str] = Field(default_factory=list)
    missing_mapping_ids: list[str] = Field(default_factory=list)
    unsupported_claim_ids: list[str] = Field(default_factory=list)
    repaired_mapping_ids: list[str] = Field(default_factory=list)
    cleaned_unsupported_paragraph_ids: list[str] = Field(default_factory=list)
    script_claim_count: int = Field(default=0, ge=0)
    manifest_claim_count_before: int = Field(default=0, ge=0)
    manifest_claim_count_after: int = Field(default=0, ge=0)
    unsupported_claim_count_before: int = Field(default=0, ge=0)
    unsupported_claim_count_after: int = Field(default=0, ge=0)
    citation_mapping_count_before: int = Field(default=0, ge=0)
    citation_mapping_count_after: int = Field(default=0, ge=0)
    citation_manifest_hash_before: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    citation_manifest_hash_after: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    final_conclusion_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    production_context_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    narrative_blueprint_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    script_draft_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    citation_validated_script_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    citation_validated_script_hash_before: str = Field(
        default="sha256:" + "0" * 64,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    citation_validated_script_hash_after: str = Field(
        default="sha256:" + "0" * 64,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    visual_plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    provider_calls: int = Field(default=0, ge=0, le=0)
    retrieval_calls: int = Field(default=0, ge=0, le=0)
    created_at: datetime
