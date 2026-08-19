from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class RetrievalStrategy(str, Enum):
    GENERAL_OPINION = "GENERAL_OPINION"
    EXPERT = "EXPERT"
    ACADEMIC = "ACADEMIC"
    GOVERNMENT = "GOVERNMENT"
    NEWS = "NEWS"
    PUBLIC_OPINION = "PUBLIC_OPINION"
    POLITICIAN = "POLITICIAN"
    INDUSTRY = "INDUSTRY"


class SearchResult(BaseModel):
    """Provider-neutral result before PRDCP assigns trace identities."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)


class RetrievedSource(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    source_id: str = Field(pattern=r"^source_[A-Za-z0-9_-]+$")
    url: HttpUrl
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    rank: int = Field(ge=1)
    query: str = Field(min_length=1)
    provider_id: str = Field(min_length=1)
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("retrieved_at must include a timezone")
        return value.astimezone(timezone.utc)


class RetrievedContext(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    retrieval_id: str = Field(pattern=r"^retrieval_[a-f0-9]{24}$")
    workflow_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    research_question_id: str | None
    agent_id: str = Field(min_length=1)
    retrieval_strategy: RetrievalStrategy
    queries: list[str] = Field(min_length=1)
    sources: list[RetrievedSource]
    limitations: list[str]
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def require_context_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("retrieved_at must include a timezone")
        return value.astimezone(timezone.utc)
