from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from researcher.schemas.research_result import CoverageStatus
from researcher.schemas.source import (
    EvidenceDirectness,
    EvidenceStance,
    ReliabilityLevel,
    ResearchSource,
    ResearchSourceType,
    SourceSpecificMetadata,
)


class ObservationType(str, Enum):
    AGREEMENT = "AGREEMENT"
    DISAGREEMENT = "DISAGREEMENT"
    SCOPE_DIFFERENCE = "SCOPE_DIFFERENCE"
    TEMPORAL_DIFFERENCE = "TEMPORAL_DIFFERENCE"


class ResearchQuestionCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    research_question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    required_categories: list[ResearchSourceType] = Field(min_length=1)
    completed_categories: list[ResearchSourceType] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    coverage_status: CoverageStatus


class CrossSourceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    observation_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(min_length=1)
    observation_type: ObservationType
    limitations: list[str] = Field(default_factory=list)


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    gap_id: str = Field(min_length=1)
    research_question_id: str = Field(min_length=1)
    missing_category: ResearchSourceType
    description: str = Field(min_length=1)


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    research_question_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)
    stance: EvidenceStance
    directness: EvidenceDirectness


class EvidenceQualityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    evidence_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    reliability: ReliabilityLevel
    directness: EvidenceDirectness
    primary_source: bool
    limitations: list[str] = Field(default_factory=list)


class SourceMetadataRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    source_id: str = Field(min_length=1)
    source_type: ResearchSourceType
    title: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    url: HttpUrl
    author_or_organization: str | None = None
    published_at: datetime | None = None
    retrieved_at: datetime
    geographic_scope: list[str] = Field(default_factory=list)
    time_scope: str | None = None
    source_specific_metadata: SourceSpecificMetadata


class SourceCategoryReferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    EXPERT: list[str] = Field(default_factory=list)
    ACADEMIC: list[str] = Field(default_factory=list)
    GOVERNMENT: list[str] = Field(default_factory=list)
    NEWS: list[str] = Field(default_factory=list)
    PUBLIC_OPINION: list[str] = Field(default_factory=list)
    POLITICIAN: list[str] = Field(default_factory=list)
    INDUSTRY: list[str] = Field(default_factory=list)


class SourceCategoryCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    EXPERT: int = Field(default=0, ge=0)
    ACADEMIC: int = Field(default=0, ge=0)
    GOVERNMENT: int = Field(default=0, ge=0)
    NEWS: int = Field(default=0, ge=0)
    PUBLIC_OPINION: int = Field(default=0, ge=0)
    POLITICIAN: int = Field(default=0, ge=0)
    INDUSTRY: int = Field(default=0, ge=0)


class ResearchReportReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    research_question_id: str | None = None
    target_agent_id: str | None = None
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)


class ResearchReportReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    findings: list[ResearchReportReviewFinding] = Field(default_factory=list)
    revision_targets: list[str] = Field(default_factory=list)


class ResearchReport(BaseModel):
    """Researcher artifact plus the canonical Researcher→Deliberation contract."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    research_report_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    research_plan_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    general_opinion: str = Field(min_length=1)
    research_questions: list[ResearchQuestionCoverage] = Field(min_length=1)
    research_scope: list[str] = Field(min_length=1)
    sources: list[ResearchSource] = Field(default_factory=list)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    source_metadata: list[SourceMetadataRecord] = Field(default_factory=list)
    source_perspectives: SourceCategoryReferences = Field(
        default_factory=SourceCategoryReferences
    )
    evidence_quality_assessments: list[EvidenceQualityAssessment] = Field(default_factory=list)
    research_limitations: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    sources_by_category: SourceCategoryReferences = Field(
        default_factory=SourceCategoryReferences
    )
    source_count_by_category: SourceCategoryCounts = Field(
        default_factory=SourceCategoryCounts
    )
    cross_source_observations: list[CrossSourceObservation] = Field(default_factory=list)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    review: ResearchReportReview | None = None

    @model_validator(mode="after")
    def ids_are_traceable(self) -> "ResearchReport":
        source_ids = {source.source_id for source in self.sources}
        evidence_ids = {source.evidence_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("sources must have unique source_id values after deduplication")
        if len(evidence_ids) != len(self.sources):
            raise ValueError("sources must have unique evidence_id values after deduplication")
        if any(item.source_id not in source_ids for item in self.evidence_items):
            raise ValueError("evidence_items contain an unknown source_id")
        if any(item.evidence_id not in evidence_ids for item in self.evidence_items):
            raise ValueError("evidence_items contain an unknown evidence_id")
        return self
