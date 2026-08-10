from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class SearchConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="ja", min_length=1)
    max_candidates: int = Field(default=3, ge=1, le=10)
    required_keywords: list[str] = Field(default_factory=lambda: ["AI"])


class TopicScoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_query: str = ""
    search_sources: list[str] = Field(default_factory=lambda: ["news", "sns", "youtube", "reddit"])
    search_constraints: SearchConstraints = Field(default_factory=SearchConstraints)
    user_topic: str | None = None
    revision_context: dict | None = None


class TopicCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    source: str = Field(min_length=1)
    url: HttpUrl
    published_at: datetime


class TopicScoutOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_candidates: list[TopicCandidate] = Field(min_length=1, max_length=10)

    @field_validator("topic_candidates")
    @classmethod
    def require_ai_candidate(cls, value: list[TopicCandidate]) -> list[TopicCandidate]:
        if not any("ai" in item.title.lower() or "ＡＩ" in item.title or "生成AI" in item.title for item in value):
            raise ValueError("Topic Candidate List must include at least one AI-related topic")
        return value

