from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deliberation.schemas.identifiers import (
    CAUSAL_ANALYSIS_PREFIX,
    EVIDENCE_PREFIXES,
    canonicalize_analysis_id,
    require_identifier_list,
)


CAUSAL_ITEM_PREFIXES = (
    "causal_",
    "mechanism_",
    "structure_",
    "structural_",
    "feedback_",
    "alternative_",
    "alt_exp_",
)
CAUSAL_ITEM_FIELDS = {
    "causal_claims": "causal_",
    "mechanisms": "mechanism_",
    "structural_factors": "structural_",
    "feedback_loops": "feedback_",
    "alternative_explanations": "alternative_",
}


class CausalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    status: str = Field(min_length=1)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            EVIDENCE_PREFIXES,
            field_name="causal_item.evidence_ids",
        )


class CausalClaimItem(CausalItem):
    item_id: str = Field(pattern=r"^causal_.+")


class MechanismItem(CausalItem):
    item_id: str = Field(pattern=r"^mechanism_.+")


class StructuralFactorItem(CausalItem):
    item_id: str = Field(pattern=r"^(?:structure_|structural_).+")


class FeedbackLoopItem(CausalItem):
    item_id: str = Field(pattern=r"^feedback_.+")


class AlternativeExplanationItem(CausalItem):
    item_id: str = Field(pattern=r"^(?:alternative_|alt_exp_).+")


class CausationRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_description: str = Field(min_length=1)
    evidence_linked: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "risk_description" in value:
            return value
        return {
            "risk_description": value.get("description"),
            "evidence_linked": value.get("evidence_ids", []),
        }


class CausalCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(min_length=1)
    evidence_linked: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_evidence_ids_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "evidence_ids" not in value:
            return value
        normalized = dict(value)
        normalized["evidence_linked"] = normalized.pop("evidence_ids")
        return normalized


class CausalEvidenceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    mapped_item_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "evidence_id" in value:
            return value
        evidence_ids = value.get("evidence_ids") or []
        return {
            "evidence_id": evidence_ids[0] if evidence_ids else "unmapped_evidence",
            "mapped_item_ids": [value.get("item_id", "unmapped_item")],
        }


class CausalStructuralAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(
        min_length=1,
        pattern=r"^causal_analysis_.+",
        description="Unique causal analysis identifier using causal_analysis_*",
    )
    task_id: str = Field(min_length=1)
    causal_claims: list[CausalClaimItem] = Field(min_length=1)
    mechanisms: list[MechanismItem] = Field(min_length=1)
    structural_factors: list[StructuralFactorItem] = Field(min_length=1)
    feedback_loops: list[FeedbackLoopItem] = Field(default_factory=list)
    alternative_explanations: list[AlternativeExplanationItem] = Field(default_factory=list)
    correlation_causation_risks: list[CausationRisk] = Field(default_factory=list)
    necessary_conditions: list[CausalCondition] = Field(default_factory=list)
    sufficient_conditions: list[CausalCondition] = Field(default_factory=list)
    evidence_mappings: list[CausalEvidenceMapping] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("analysis_id", mode="before")
    @classmethod
    def normalize_analysis_id(cls, value: str) -> str:
        return canonicalize_analysis_id(
            value,
            canonical_prefix=CAUSAL_ANALYSIS_PREFIX,
            legacy_prefixes=("analysis_causal_",),
        )

    @model_validator(mode="after")
    def validate_unique_items(self) -> "CausalStructuralAnalysisResult":
        items = (
            self.causal_claims
            + self.mechanisms
            + self.structural_factors
            + self.feedback_loops
            + self.alternative_explanations
        )
        ids = [item.item_id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("causal and structural item IDs must be unique")
        return self


def canonicalize_legacy_causal_item_ids(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize a saved pre-v2 Causal result without rewriting its JSON file."""

    normalized = deepcopy(value)
    replacements: dict[str, str] = {}
    for field_name, prefix in CAUSAL_ITEM_FIELDS.items():
        for item in normalized.get(field_name, []):
            if not isinstance(item, dict):
                continue
            identifier = item.get("item_id")
            if not isinstance(identifier, str):
                continue
            if field_name == "structural_factors":
                valid_for_field = identifier.startswith(("structure_", "structural_"))
            elif field_name == "alternative_explanations":
                # Both namespaces are canonical for primary causal alternatives.
                # Do not rewrite alt_exp_* as a legacy alias during checkpoint reads.
                valid_for_field = identifier.startswith(("alternative_", "alt_exp_"))
            else:
                valid_for_field = identifier.startswith(prefix)
            if valid_for_field:
                continue
            digest = hashlib.sha256(
                f"{field_name}|{identifier}".encode("utf-8")
            ).hexdigest()[:20]
            replacements[identifier] = f"{prefix}legacy_{digest}"
            item["item_id"] = replacements[identifier]
    for mapping in normalized.get("evidence_mappings", []):
        if not isinstance(mapping, dict):
            continue
        mapping["mapped_item_ids"] = [
            replacements.get(identifier, identifier)
            for identifier in mapping.get("mapped_item_ids", [])
        ]
    return normalized


def canonicalize_legacy_causal_references(
    value: dict[str, Any],
    causal_analysis: dict[str, Any],
) -> dict[str, Any]:
    """Rewrite references only when an authoritative saved Causal item provides a map."""

    canonical = canonicalize_legacy_causal_item_ids(causal_analysis)
    replacements: dict[str, str] = {}
    for field_name in CAUSAL_ITEM_FIELDS:
        original_items = causal_analysis.get(field_name, [])
        canonical_items = canonical.get(field_name, [])
        for original, normalized in zip(original_items, canonical_items, strict=True):
            if isinstance(original, dict) and isinstance(normalized, dict):
                old_id = original.get("item_id")
                new_id = normalized.get("item_id")
                if isinstance(old_id, str) and isinstance(new_id, str):
                    replacements[old_id] = new_id

    def replace(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: replace(child) for key, child in item.items()}
        if isinstance(item, list):
            return [replace(child) for child in item]
        if isinstance(item, str):
            return replacements.get(item, item)
        return item

    return replace(value)
