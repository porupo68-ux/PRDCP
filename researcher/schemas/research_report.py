from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from researcher.schemas.research_result import CoverageStatus
from researcher.schemas.source import (
    EvidenceDirectness,
    EvidenceStance,
    ReliabilityLevel,
    ResearchSource,
    ResearchSourceType,
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
    source_metadata: list[dict[str, Any]] = Field(default_factory=list)
    source_perspectives: dict[str, list[str]] = Field(default_factory=dict)
    evidence_quality_assessments: list[EvidenceQualityAssessment] = Field(default_factory=list)
    research_limitations: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    sources_by_category: dict[str, list[str]] = Field(default_factory=dict)
    source_count_by_category: dict[str, int] = Field(default_factory=dict)
    cross_source_observations: list[CrossSourceObservation] = Field(default_factory=list)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    review: dict[str, Any] | None = None

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
