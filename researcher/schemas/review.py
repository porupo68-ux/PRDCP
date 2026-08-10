from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from producer.schemas.research_plan import ResearchPlan
from researcher.schemas.research_report import ResearchReport
from researcher.schemas.research_task import RESEARCHER_AGENT_IDS


class ResearchQualityGateDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class FindingSeverity(str, Enum):
    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class ResearchReviewFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    finding_id: str = Field(min_length=1)
    severity: FindingSeverity
    research_question_id: str | None = None
    target_agent_id: str | None = None
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_target(self) -> "ResearchReviewFinding":
        if self.target_agent_id is not None and self.target_agent_id not in RESEARCHER_AGENT_IDS:
            raise ValueError("finding target_agent_id must identify a specialist Researcher")
        return self


class ResearchQualityReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_plan: ResearchPlan
    research_report: ResearchReport
    revision_context: dict | None = None


class ResearchQualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: ResearchQualityGateDecision
    reason: str = Field(min_length=1)
    findings: list[ResearchReviewFinding] = Field(default_factory=list)
    revision_targets: list[str] = Field(default_factory=list)
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
        elif self.status == ResearchQualityGateDecision.REVISION_REQUIRED:
            if not self.findings or not self.revision_targets:
                raise ValueError("revision_required must include findings and revision_targets")
            if self.approved_research_report is not None:
                raise ValueError("revision_required cannot approve a report")
        elif self.approved_research_report is not None:
            raise ValueError("blocked review cannot approve a report")
        return self
