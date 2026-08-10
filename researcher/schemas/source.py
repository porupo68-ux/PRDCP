from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class ResearchSourceType(str, Enum):
    EXPERT = "EXPERT"
    ACADEMIC = "ACADEMIC"
    GOVERNMENT = "GOVERNMENT"
    NEWS = "NEWS"
    PUBLIC_OPINION = "PUBLIC_OPINION"
    POLITICIAN = "POLITICIAN"
    INDUSTRY = "INDUSTRY"


class EvidenceStance(str, Enum):
    SUPPORTS = "SUPPORTS"
    OPPOSES = "OPPOSES"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ReliabilityLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class EvidenceDirectness(str, Enum):
    DIRECT = "DIRECT"
    INDIRECT = "INDIRECT"
    CONTEXTUAL = "CONTEXTUAL"
    UNKNOWN = "UNKNOWN"


REQUIRED_METADATA = {
    ResearchSourceType.EXPERT: {"expert_name", "field", "affiliation", "statement_context"},
    ResearchSourceType.ACADEMIC: {"doi", "peer_reviewed", "journal_name", "study_type"},
    ResearchSourceType.GOVERNMENT: {"organization", "country", "document_type"},
    ResearchSourceType.NEWS: {"media_name", "article_type"},
    ResearchSourceType.PUBLIC_OPINION: {
        "platform",
        "engagement_count",
        "sample_size",
        "representativeness_warning",
    },
    ResearchSourceType.POLITICIAN: {
        "politician_name",
        "party",
        "position",
        "statement_type",
    },
    ResearchSourceType.INDUSTRY: {
        "organization_name",
        "organization_type",
        "industry",
    },
}


class ResearchSource(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    source_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    research_question_ids: list[str] = Field(min_length=1)
    source_type: ResearchSourceType
    title: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    url: HttpUrl
    author_or_organization: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    summary: str = Field(min_length=1)
    relevant_excerpt: str | None = None
    stance: EvidenceStance = EvidenceStance.UNKNOWN
    reliability: ReliabilityLevel = ReliabilityLevel.UNKNOWN
    directness: EvidenceDirectness = EvidenceDirectness.UNKNOWN
    primary_source: bool
    geographic_scope: list[str] = Field(default_factory=list)
    time_scope: str | None = None
    limitations: list[str] = Field(default_factory=list)
    source_specific_metadata: dict[str, Any]

    @field_validator("research_question_ids")
    @classmethod
    def unique_question_ids(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("research_question_ids cannot contain empty values")
        return list(dict.fromkeys(value))

    @field_validator("published_at", "retrieved_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("source timestamps must include a timezone")
        return value.astimezone(timezone.utc) if value is not None else None

    @model_validator(mode="after")
    def validate_category_metadata(self) -> "ResearchSource":
        required = REQUIRED_METADATA[ResearchSourceType(self.source_type)]
        missing = sorted(required - self.source_specific_metadata.keys())
        if missing:
            raise ValueError(
                f"{self.source_type} source_specific_metadata is missing: {', '.join(missing)}"
            )
        return self
