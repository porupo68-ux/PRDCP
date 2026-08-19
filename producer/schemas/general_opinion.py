from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from producer.schemas.topic_selector import SelectedTopic


class GeneralOpinionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_topic: SelectedTopic
    revision_context: dict | None = None


class SupportingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional only for legacy saved Producer artifacts.  The provider-bound
    # specialized schema always requires a compact retrieved source ID.
    source_id: str | None = None
    source: str = Field(min_length=1)
    url: HttpUrl


class GeneralOpinion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general_opinion_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence_summary: str = Field(min_length=1)
    supporting_sources: list[SupportingSource] = Field(min_length=3)


class GeneralOpinionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    general_opinion: GeneralOpinion

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        sources = input_data.get("retrieval_context", {}).get("sources", [])
        if not isinstance(sources, list) or not sources:
            return schema
        definitions = schema.get("$defs", {})
        supporting = definitions.get("SupportingSource")
        if not isinstance(supporting, dict):
            return schema
        source_ids = [
            item.get("source_id")
            for item in sources
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        if source_ids:
            # Provider constraint grammars handle short IDs predictably.  Raw
            # titles/URLs remain in retrieval_context and are cross-checked by
            # the application after generation instead of being compiled into
            # potentially unbounded enum literals.
            supporting["properties"]["source_id"] = {
                "type": "string",
                "enum": source_ids,
            }
            # Title and URL are immutable Retrieval metadata.  Asking the LLM
            # to reproduce a 1,801-character PDF title exactly caused a paid
            # response to fail local equality validation even after Gemini
            # accepted the repaired schema.  The agent hydrates these fields
            # deterministically from source_id after generation.
            supporting["properties"].pop("source", None)
            supporting["properties"].pop("url", None)
            supporting["required"] = list(supporting["properties"])
            schema["$defs"]["GeneralOpinion"]["properties"][
                "supporting_sources"
            ]["maxItems"] = len(source_ids)
        return schema
