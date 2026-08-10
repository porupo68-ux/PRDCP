from pydantic import BaseModel, ConfigDict, Field

from producer.schemas.topic_scout import TopicCandidate


class TopicSelectorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_candidates: list[TopicCandidate] = Field(min_length=1)
    revision_context: dict | None = None


class SelectedTopic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)


class TopicSelectorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_topic: SelectedTopic

