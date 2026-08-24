from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deliberation.schemas.identifiers import (
    EVIDENCE_PREFIXES,
    SOURCE_PREFIXES,
    STAKEHOLDER_ANALYSIS_PREFIX,
    canonicalize_analysis_id,
    require_identifier_list,
)


_SPECIFIC_INFORMATION_PATTERN = re.compile(
    r"(?:\d[\d,.]*\s*(?:%|％|人|件|億|万|兆)?)|"
    r"(?:省|庁|局|連合会|株式会社|大学|研究所|新聞|テレビ|協会|財団|政党)"
)


class SpecificFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    verification_status: Literal["verified", "inferred", "unknown", "unverified"]
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    research_gap: str = ""

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            EVIDENCE_PREFIXES,
            field_name="specific_fact.evidence_ids",
        )

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            SOURCE_PREFIXES,
            field_name="specific_fact.source_ids",
        )

    @model_validator(mode="after")
    def validate_verification(self) -> "SpecificFact":
        allowed = {"verified", "inferred", "unknown", "unverified"}
        if self.verification_status not in allowed:
            raise ValueError(
                f"verification_status must be one of {sorted(allowed)}"
            )
        if self.verification_status in {"verified", "inferred"}:
            if not self.evidence_ids or not self.source_ids:
                raise ValueError(
                    "verified/inferred specific facts require evidence_ids and source_ids"
                )
        elif not self.research_gap:
            raise ValueError(
                "unknown/unverified specific facts require an explicit research_gap"
            )
        return self


class Stakeholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stakeholder_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class StakeholderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    stakeholder_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class ExistingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(min_length=1)
    actor_stakeholder_ids: list[str] = Field(min_length=1)
    description: str = Field(min_length=1)
    implementation_status: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class ResponseEffectiveness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(default="unspecified_result", min_length=1)
    response_id: str | None = None
    description: str = Field(min_length=1)
    effectiveness: str = Field(default="unknown", min_length=1)
    observed_changes: str = ""
    limitations: str = ""
    side_effects: str = ""
    causal_attribution_status: str = Field(default="unknown", min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    target_problems: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_assessment_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "assessment" not in value:
            return value
        normalized = dict(value)
        normalized["description"] = normalized.pop("assessment")
        return normalized


class StakeholderIncentive(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(default="unspecified_incentive", min_length=1)
    stakeholder_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    explicit_vs_inferred: str = Field(default="unspecified", min_length=1)


class ImplementationBarrier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(default="unspecified_barrier", min_length=1)
    stakeholder_id: str | None = None
    response_id: str | None = None
    description: str = Field(min_length=1)
    type: str = Field(default="unspecified", min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class DistributionalEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(default="unspecified_effect", min_length=1)
    stakeholder_id: str | None = None
    affected_group: str = ""
    effect_type: str = Field(default="unspecified", min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: str = ""


class MappedStakeholderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    item_type: str = Field(min_length=1)


class StakeholderEvidenceMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1)
    mapped_items: list[MappedStakeholderItem] = Field(default_factory=list)
    research_question_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "evidence_id" in value:
            return value
        evidence_ids = value.get("evidence_ids") or []
        return {
            "evidence_id": evidence_ids[0] if evidence_ids else "unmapped_evidence",
            "mapped_items": [
                {
                    "item_id": value.get("item_id", "unmapped_item"),
                    "item_type": "unspecified",
                }
            ],
            "research_question_ids": [],
        }


class StakeholderResponseAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(
        min_length=1,
        pattern=r"^stakeholder_analysis_.+",
        description="Unique stakeholder analysis identifier using stakeholder_analysis_*",
    )
    task_id: str = Field(min_length=1)
    stakeholders: list[Stakeholder] = Field(min_length=1)
    interests: list[StakeholderItem] = Field(default_factory=list)
    authority_and_capacity: list[StakeholderItem] = Field(min_length=1)
    existing_responses: list[ExistingResponse] = Field(default_factory=list)
    response_effectiveness: list[ResponseEffectiveness] = Field(default_factory=list)
    incentives: list[StakeholderIncentive] = Field(default_factory=list)
    implementation_barriers: list[ImplementationBarrier] = Field(default_factory=list)
    distributional_effects: list[DistributionalEffect] = Field(default_factory=list)
    evidence_mappings: list[StakeholderEvidenceMapping] = Field(min_length=1)
    specific_facts: list[SpecificFact] = Field(default_factory=list)
    research_gaps: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind trace IDs to the Evidence records supplied in this assignment."""

        context = input_data.get("evidence_context")
        if not isinstance(context, list) or not context:
            return schema
        evidence_ids = list(
            dict.fromkeys(
                item.get("evidence_id")
                for item in context
                if isinstance(item, dict)
                and isinstance(item.get("evidence_id"), str)
            )
        )
        source_ids = list(
            dict.fromkeys(
                item.get("source_id")
                for item in context
                if isinstance(item, dict)
                and isinstance(item.get("source_id"), str)
            )
        )
        task_id = input_data.get("task_id")
        definitions = schema.setdefault("$defs", {})
        if not isinstance(definitions, dict):
            raise ValueError("Stakeholder strict schema has no usable $defs")
        definitions["AssignedEvidenceId"] = {
            "type": "string",
            "enum": evidence_ids,
        }
        definitions["AssignedSourceId"] = {
            "type": "string",
            "enum": source_ids,
        }
        evidence_reference = {"$ref": "#/$defs/AssignedEvidenceId"}
        source_reference = {"$ref": "#/$defs/AssignedSourceId"}

        def bind(node: Any) -> None:
            if isinstance(node, dict):
                properties = node.get("properties")
                if isinstance(properties, dict):
                    for field_name, field_schema in properties.items():
                        if not isinstance(field_schema, dict):
                            continue
                        if field_name == "evidence_ids" and evidence_ids:
                            field_schema["items"] = dict(evidence_reference)
                        elif field_name == "evidence_id" and evidence_ids:
                            properties[field_name] = dict(evidence_reference)
                        elif field_name == "source_ids" and source_ids:
                            field_schema["items"] = dict(source_reference)
                        elif (
                            field_name == "task_id"
                            and isinstance(task_id, str)
                            and task_id
                        ):
                            properties[field_name] = {
                                "type": "string",
                                "enum": [task_id],
                            }
                for child in node.values():
                    bind(child)
            elif isinstance(node, list):
                for child in node:
                    bind(child)

        bind(schema)
        return schema

    @model_validator(mode="before")
    @classmethod
    def downgrade_legacy_specifics(cls, value: object) -> object:
        if not isinstance(value, dict) or "specific_facts" in value:
            return value
        analysis_id = str(value.get("analysis_id", ""))
        if analysis_id.startswith(STAKEHOLDER_ANALYSIS_PREFIX):
            return value
        normalized = dict(value)
        statements = _collect_specific_statements(normalized)
        normalized["specific_facts"] = [
            {
                "fact_id": f"legacy_specific_{index}",
                "statement": statement,
                "verification_status": "unverified",
                "evidence_ids": [],
                "source_ids": [],
                "research_gap": "Legacy checkpoint did not preserve content-level evidence verification",
            }
            for index, statement in enumerate(statements, start=1)
        ]
        normalized["research_gaps"] = list(
            dict.fromkeys(
                [
                    *normalized.get("research_gaps", []),
                    *(
                        ["Legacy checkpoint did not preserve content-level evidence verification"]
                        if statements
                        else []
                    ),
                ]
            )
        )
        return normalized

    @field_validator("analysis_id", mode="before")
    @classmethod
    def normalize_analysis_id(cls, value: str) -> str:
        return canonicalize_analysis_id(
            value,
            canonical_prefix=STAKEHOLDER_ANALYSIS_PREFIX,
            legacy_prefixes=("analysis_stakeholder_", "analysis_task_"),
        )

    @model_validator(mode="after")
    def validate_stakeholders(self) -> "StakeholderResponseAnalysisResult":
        ids = [item.stakeholder_id for item in self.stakeholders]
        if len(set(ids)) != len(ids):
            raise ValueError("stakeholders must have unique stakeholder_id values")
        known = set(ids)
        if any(item.stakeholder_id not in known for item in self.interests):
            raise ValueError("interests reference an unknown stakeholder_id")
        if any(item.stakeholder_id not in known for item in self.authority_and_capacity):
            raise ValueError("authority_and_capacity references an unknown stakeholder_id")
        concrete_statements = _collect_unbound_specific_statements(
            self.model_dump(mode="json", exclude={"specific_facts", "research_gaps"})
        )
        uncovered = [
            statement
            for statement in concrete_statements
            if not any(
                fact.statement == statement
                or fact.statement in statement
                or statement in fact.statement
                for fact in self.specific_facts
            )
        ]
        if uncovered:
            raise ValueError(
                "specific names/numbers require SpecificFact verification records: "
                f"{uncovered}"
            )
        missing_gaps = {
            fact.research_gap
            for fact in self.specific_facts
            if fact.verification_status in {"unknown", "unverified"}
            and fact.research_gap not in self.research_gaps
        }
        if missing_gaps:
            raise ValueError(
                "unverified SpecificFact research_gap values must be listed in research_gaps"
            )
        return self


def _collect_specific_statements(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_id") or key.endswith("_ids"):
                continue
            found.extend(_collect_specific_statements(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_specific_statements(item))
    elif isinstance(value, str) and _SPECIFIC_INFORMATION_PATTERN.search(value):
        found.append(value)
    return list(dict.fromkeys(found))


def _collect_unbound_specific_statements(value: Any) -> list[str]:
    """Find concrete text that has neither a local Evidence link nor a fact record."""

    found: list[str] = []
    if isinstance(value, dict):
        locally_bound = bool(value.get("evidence_ids")) or bool(value.get("evidence_id"))
        for key, item in value.items():
            if key.endswith("_id") or key.endswith("_ids"):
                continue
            if key in {"uncertainties", "research_gaps", "limitations"}:
                continue
            if isinstance(item, str):
                if not locally_bound and _SPECIFIC_INFORMATION_PATTERN.search(item):
                    found.append(item)
            else:
                found.extend(_collect_unbound_specific_statements(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_unbound_specific_statements(item))
    return list(dict.fromkeys(found))
