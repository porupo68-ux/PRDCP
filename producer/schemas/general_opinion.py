from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from producer.schemas.topic_selector import SelectedTopic


class GeneralOpinionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_topic: SelectedTopic
    revision_context: dict | None = None


class SupportingSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
