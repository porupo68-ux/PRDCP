from __future__ import annotations

from copy import deepcopy
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from producer.schemas.research_plan import ResearchTarget
from researcher.schemas.research_task import RESEARCHER_AGENT_IDS, RESEARCH_TARGET_MAP
from researcher.schemas.source import (
    PROVENANCE_HYDRATED_METADATA,
    SOURCE_METADATA_MODELS,
    ResearchSource,
    ResearchSourceType,
)


class CoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NO_RESULT = "NO_RESULT"


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    task_id: str = Field(min_length=1)
    research_question_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    sources: list[ResearchSource] = Field(default_factory=list)
    search_summary: str = Field(min_length=1)
    coverage_status: CoverageStatus
    limitations: list[str] = Field(default_factory=list)

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind the shared root schema to the Research Task's one source category."""

        raw_target = input_data.get("research_target")
        raw_agent = input_data.get("target_agent_id")
        if raw_target is None or raw_agent is None:
            return schema
        target = ResearchTarget(raw_target)
        expected_agent = RESEARCH_TARGET_MAP[target]
        if raw_agent != expected_agent:
            raise ValueError(
                f"research_target {target.value} must route to {expected_agent}"
            )
        definitions = schema.get("$defs")
        if not isinstance(definitions, dict):
            raise ValueError("ResearchResult strict schema has no $defs")
        source_schema = definitions.get("ResearchSource")
        branches = source_schema.get("anyOf") if isinstance(source_schema, dict) else None
        if not isinstance(branches, list):
            raise ValueError("ResearchResult strict schema has no source-type branches")
        selected = next(
            (
                branch
                for branch in branches
                if branch.get("properties", {})
                .get("source_type", {})
                .get("const")
                == target.value
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                f"ResearchResult strict schema has no {target.value} source branch"
            )
        definitions["ResearchSource"] = deepcopy(selected)
        source_type = ResearchSourceType(target.value)
        metadata_model_name = SOURCE_METADATA_MODELS[source_type].__name__
        metadata_schema = definitions.get(metadata_model_name)
        if not isinstance(metadata_schema, dict):
            raise ValueError(
                f"ResearchResult strict schema has no {metadata_model_name} definition"
            )
        metadata_properties = metadata_schema.get("properties")
        if not isinstance(metadata_properties, dict):
            raise ValueError(
                f"ResearchResult strict schema {metadata_model_name} has no properties"
            )
        for field_name in PROVENANCE_HYDRATED_METADATA[source_type]:
            metadata_properties.pop(field_name, None)
        metadata_schema["required"] = list(metadata_properties)
        retrieval_sources = input_data.get("retrieval_context", {}).get("sources", [])
        if isinstance(retrieval_sources, list):
            source_ids = [
                item.get("source_id")
                for item in retrieval_sources
                if isinstance(item, dict) and isinstance(item.get("source_id"), str)
            ]
            source_properties = definitions["ResearchSource"].get("properties", {})
            if source_ids:
                source_properties["source_id"] = {
                    "type": "string",
                    "enum": source_ids,
                }
            # These values are immutable or redundant properties of the
            # persisted Retrieval record.  The model selects source_id and
            # produces analytical fields only; the agent restores immutable
            # metadata and provenance before Pydantic/local contract validation.
            for field_name in (
                "title",
                "source_name",
                "url",
                "author_or_organization",
                "published_at",
                "retrieved_at",
                "relevant_excerpt",
            ):
                source_properties.pop(field_name, None)
            definitions["ResearchSource"]["required"] = list(source_properties)
            schema["properties"]["sources"]["maxItems"] = len(source_ids)
        schema["properties"]["agent_id"] = {
            "type": "string",
            "enum": [expected_agent],
        }
        source_type_schema = definitions["ResearchSource"]["properties"]["source_type"]
        source_type_schema.pop("const", None)
        source_type_schema["enum"] = [target.value]
        return schema

    @model_validator(mode="after")
    def validate_coverage(self) -> "ResearchResult":
        if self.agent_id not in RESEARCHER_AGENT_IDS:
            raise ValueError("agent_id must identify a specialist Researcher")
        expected_source_type = next(
            target.value
            for target, agent_id in RESEARCH_TARGET_MAP.items()
            if agent_id == self.agent_id
        )
        mismatched = [
            source.source_id
            for source in self.sources
            if source.source_type != expected_source_type
        ]
        if mismatched:
            raise ValueError(
                f"{self.agent_id} may return only {expected_source_type} sources; "
                f"mismatched source_ids: {', '.join(mismatched)}"
            )
        if self.coverage_status == CoverageStatus.COMPLETE and not self.sources:
            raise ValueError("COMPLETE result must include at least one source")
        if self.coverage_status == CoverageStatus.NO_RESULT and self.sources:
            raise ValueError("NO_RESULT cannot include sources")
        for source in self.sources:
            if self.research_question_id not in source.research_question_ids:
                raise ValueError("every source must trace to the result research_question_id")
        return self
