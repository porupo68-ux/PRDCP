from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from producer.schemas.research_plan import ResearchPlan
from researcher.schemas.research_report import ResearchReport
from researcher.schemas.research_task import RESEARCHER_AGENT_IDS
from researcher.schemas.human_evidence import ResearchFindingType


class ResearchQualityGateDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class FindingSeverity(str, Enum):
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class ResearchFindingTarget(str, Enum):
    MANAGER = "researcher.manager"
    EXPERT = "researcher.expert_researcher"
    ACADEMIC = "researcher.academic_researcher"
    GOVERNMENT = "researcher.government_researcher"
    NEWS = "researcher.news_researcher"
    PUBLIC_OPINION = "researcher.public_opinion_researcher"
    POLITICIAN = "researcher.politician_researcher"
    INDUSTRY = "researcher.industry_researcher"


class ResearchRevisionTarget(str, Enum):
    EXPERT = "researcher.expert_researcher"
    ACADEMIC = "researcher.academic_researcher"
    GOVERNMENT = "researcher.government_researcher"
    NEWS = "researcher.news_researcher"
    PUBLIC_OPINION = "researcher.public_opinion_researcher"
    POLITICIAN = "researcher.politician_researcher"
    INDUSTRY = "researcher.industry_researcher"


class ResearchReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    finding_id: str = Field(min_length=1)
    finding_type: ResearchFindingType = ResearchFindingType.UNCLASSIFIED
    severity: FindingSeverity
    research_question_id: str | None = None
    target_agent_id: ResearchFindingTarget | None = None
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target(self) -> "ResearchReviewFinding":
        if self.target_agent_id is not None and self.target_agent_id not in {
            "researcher.manager",
            *RESEARCHER_AGENT_IDS,
        }:
            raise ValueError(
                "finding target_agent_id must identify Researcher Manager or a specialist"
            )
        return self


class ResearchQualityReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    research_plan: ResearchPlan
    research_report: ResearchReport
    revision_context: dict | None = None


class ResearchQualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: ResearchQualityGateDecision
    reason: str = Field(min_length=1)
    findings: list[ResearchReviewFinding] = Field(default_factory=list)
    revision_targets: list[ResearchRevisionTarget] = Field(default_factory=list)
    approved_research_report: ResearchReport | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> "ResearchQualityReviewOutput":
        invalid_targets = set(self.revision_targets) - RESEARCHER_AGENT_IDS
        if invalid_targets:
            raise ValueError(f"invalid revision targets: {sorted(invalid_targets)}")
        if self.status in {
            ResearchQualityGateDecision.APPROVED,
            ResearchQualityGateDecision.APPROVED_WITH_CONDITIONS,
        }:
            if self.approved_research_report is None:
                raise ValueError("approved review must include approved_research_report")
            if self.revision_targets:
                raise ValueError("approved review cannot route revision targets")
            if any(
                item.finding_type == ResearchFindingType.HARD_INTEGRITY_FAILURE.value
                for item in self.findings
            ):
                raise ValueError("approved review cannot contain a hard integrity failure")
        elif self.status == ResearchQualityGateDecision.REVISION_REQUIRED:
            if not self.findings:
                raise ValueError("revision_required must include findings")
            executable_targets = {
                str(item.target_agent_id)
                for item in self.findings
                if item.finding_type
                == ResearchFindingType.EVIDENCE_SUFFICIENCY.value
                and item.target_agent_id not in {None, "researcher.manager"}
            }
            if executable_targets - set(self.revision_targets):
                raise ValueError(
                    "revision_targets must cover every executable evidence finding"
                )
            if self.approved_research_report is not None:
                raise ValueError("revision_required cannot approve a report")
        elif self.approved_research_report is not None:
            raise ValueError("blocked review cannot approve a report")
        return self
