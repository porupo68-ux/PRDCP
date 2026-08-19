from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from researcher.schemas.research_report import (
    CrossSourceObservation,
    EvidenceGap,
    ResearchQuestionCoverage,
    ResearchReport,
    SourceCategoryCounts,
)
from researcher.schemas.human_evidence import (
    AcceptedEvidenceGap,
    HumanEvidenceDecision,
    HumanEvidenceIntegrityRepair,
)
from researcher.schemas.source import (
    EvidenceDirectness,
    EvidenceStance,
    ReliabilityLevel,
    ResearchSourceType,
    SourceSpecificMetadata,
)
from researcher.schemas.trace_ids import EvidenceId, SourceId


class DeliberationEvidenceContext(BaseModel):
    """One non-duplicated evidence/provenance record for LLM deliberation."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    evidence_id: EvidenceId
    source_id: SourceId
    research_question_ids: list[str] = Field(min_length=1)
    source_type: ResearchSourceType
    title: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    url: HttpUrl
    author_or_organization: str | None
    published_at: datetime | None
    retrieved_at: datetime
    summary: str = Field(min_length=1)
    relevant_excerpt: str | None
    stance: EvidenceStance
    reliability: ReliabilityLevel
    directness: EvidenceDirectness
    primary_source: bool
    geographic_scope: list[str]
    time_scope: str | None
    limitations: list[str]
    source_specific_metadata: SourceSpecificMetadata


class DeliberationResearchContext(BaseModel):
    """Meaning-preserving provider view of an approved ResearchReport.

    The canonical ResearchReport remains the stored artifact. This model merges the
    source, evidence, metadata, and quality tables by their trace IDs so the same
    prose is not sent four times to every downstream LLM stage.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    research_report_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    research_plan_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    general_opinion: str = Field(min_length=1)
    research_questions: list[ResearchQuestionCoverage] = Field(min_length=1)
    research_scope: list[str] = Field(min_length=1)
    evidence_items: list[DeliberationEvidenceContext] = Field(min_length=1)
    research_limitations: list[str]
    unresolved_questions: list[str]
    source_count_by_category: SourceCategoryCounts
    cross_source_observations: list[CrossSourceObservation]
    evidence_gaps: list[EvidenceGap]
    research_review_status: str = Field(min_length=1)
    research_review_reason: str = Field(min_length=1)
    human_evidence_decision: HumanEvidenceDecision | None = None
    accepted_evidence_gaps: list[AcceptedEvidenceGap] = Field(default_factory=list)
    human_evidence_integrity_repairs: list[HumanEvidenceIntegrityRepair] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_trace_ids(self) -> "DeliberationResearchContext":
        evidence_ids = [item.evidence_id for item in self.evidence_items]
        source_ids = [item.source_id for item in self.evidence_items]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Deliberation evidence context contains duplicate evidence_ids")
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("Deliberation evidence context contains duplicate source_ids")
        known_evidence_ids = set(evidence_ids)
        if any(
            set(item.evidence_ids) - known_evidence_ids
            for item in self.research_questions
        ):
            raise ValueError("research question context references unknown evidence_ids")
        if any(
            set(item.supporting_evidence_ids) - known_evidence_ids
            for item in self.cross_source_observations
        ):
            raise ValueError("cross-source context references unknown evidence_ids")
        if self.human_evidence_decision is not None:
            accepted_ids = {
                item.finding_id for item in self.accepted_evidence_gaps
            }
            if accepted_ids != set(
                self.human_evidence_decision.accepted_finding_ids
            ):
                raise ValueError(
                    "accepted evidence gaps do not match the Human Evidence Decision"
                )
        return self


def build_deliberation_research_context(
    report: ResearchReport,
    *,
    human_evidence_decision: HumanEvidenceDecision | None = None,
    accepted_evidence_gaps: list[AcceptedEvidenceGap] | None = None,
    human_evidence_integrity_repairs: list[HumanEvidenceIntegrityRepair] | None = None,
) -> DeliberationResearchContext:
    """Build the runtime view without mutating or rewriting the stored report."""

    source_by_id = {item.source_id: item for item in report.sources}
    metadata_by_id = {item.source_id: item for item in report.source_metadata}
    quality_by_evidence = {
        item.evidence_id: item for item in report.evidence_quality_assessments
    }
    records: list[DeliberationEvidenceContext] = []
    evidence_local_limitations: set[str] = set()
    for evidence in report.evidence_items:
        source = source_by_id[evidence.source_id]
        metadata = metadata_by_id.get(evidence.source_id)
        quality = quality_by_evidence.get(evidence.evidence_id)
        limitations = list(
            dict.fromkeys(
                [
                    *source.limitations,
                    *(quality.limitations if quality is not None else []),
                ]
            )
        )
        evidence_local_limitations.update(limitations)
        records.append(
            DeliberationEvidenceContext(
                evidence_id=evidence.evidence_id,
                source_id=evidence.source_id,
                research_question_ids=evidence.research_question_ids,
                source_type=source.source_type,
                title=source.title,
                source_name=source.source_name,
                url=source.url,
                author_or_organization=source.author_or_organization,
                published_at=source.published_at,
                retrieved_at=source.retrieved_at,
                summary=evidence.summary,
                relevant_excerpt=source.relevant_excerpt,
                stance=evidence.stance,
                reliability=(quality.reliability if quality else source.reliability),
                directness=(quality.directness if quality else evidence.directness),
                primary_source=(quality.primary_source if quality else source.primary_source),
                geographic_scope=(
                    metadata.geographic_scope if metadata else source.geographic_scope
                ),
                time_scope=metadata.time_scope if metadata else source.time_scope,
                limitations=limitations,
                source_specific_metadata=source.source_specific_metadata,
            )
        )

    review = report.review
    return DeliberationResearchContext(
        research_report_id=report.research_report_id,
        workflow_id=report.workflow_id,
        research_plan_id=report.research_plan_id,
        topic=report.topic,
        general_opinion=report.general_opinion,
        research_questions=report.research_questions,
        research_scope=report.research_scope,
        evidence_items=records,
        research_limitations=[
            item
            for item in report.research_limitations
            if item not in evidence_local_limitations
        ],
        unresolved_questions=report.unresolved_questions,
        source_count_by_category=report.source_count_by_category,
        cross_source_observations=report.cross_source_observations,
        evidence_gaps=report.evidence_gaps,
        research_review_status=(review.status if review else "unavailable"),
        research_review_reason=(review.reason if review else "No review artifact supplied"),
        human_evidence_decision=human_evidence_decision,
        accepted_evidence_gaps=accepted_evidence_gaps or [],
        human_evidence_integrity_repairs=human_evidence_integrity_repairs or [],
    )
