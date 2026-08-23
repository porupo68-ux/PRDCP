from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from common.models.pmp import PMPMessage
from common.models.revision import RevisionControlState
from common.models.workflow import WorkflowStatus
from researcher.schemas.human_evidence import (
    AcceptedEvidenceGap,
    EvidenceRevisionPlan,
    HumanEvidenceDecision,
    HumanEvidenceIntegrityRepairRecord,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    target_agent_ids: list[str] = Field(min_length=1)
    findings: list[dict[str, Any]] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class ExternalResearchRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    iteration: int = Field(ge=1)
    source_agent_id: str = Field(min_length=1)
    parent_message_id: str = Field(min_length=1)
    revision_request_ids: list[str] = Field(min_length=1)
    target_agent_ids: list[str] = Field(min_length=1)
    status: Literal["processing", "completed", "reply_sent", "blocked", "failed"]
    created_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    reply_message_id: str | None = None


class ExternalRevisionCheckpoint(str, Enum):
    REQUEST_RECEIVED = "REQUEST_RECEIVED"
    TASKS_PLANNED = "TASKS_PLANNED"
    TASKS_DISPATCHED = "TASKS_DISPATCHED"
    RESULTS_COLLECTED = "RESULTS_COLLECTED"
    REPORT_INTEGRATING = "REPORT_INTEGRATING"
    REPORT_INTEGRATED = "REPORT_INTEGRATED"
    QUALITY_REVIEWING = "QUALITY_REVIEWING"
    QUALITY_REVIEWED = "QUALITY_REVIEWED"
    REPLY_READY = "REPLY_READY"
    COMPLETED_REVISION = "COMPLETED_REVISION"
    # Read compatibility for states written by the first recovery implementation.
    RESEARCH_DISPATCHED = "RESEARCH_DISPATCHED"
    RESEARCH_RESULTS_COLLECTED = "RESEARCH_RESULTS_COLLECTED"


class ResearcherWorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_assignment=True)

    workflow_id: str
    status: WorkflowStatus = WorkflowStatus.CREATED
    current_agent_ids: list[str] = Field(default_factory=list)
    producer_handoff: dict[str, Any]
    research_plan: dict[str, Any]
    research_tasks: list[dict[str, Any]] = Field(default_factory=list)
    agent_results: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    collected_sources: list[dict[str, Any]] = Field(default_factory=list)
    research_report: dict[str, Any] | None = None
    review_result: dict[str, Any] | None = None
    human_evidence_decision: HumanEvidenceDecision | None = None
    human_evidence_decision_history: list[HumanEvidenceDecision] = Field(
        default_factory=list
    )
    accepted_evidence_gaps: list[AcceptedEvidenceGap] = Field(default_factory=list)
    human_evidence_integrity_repairs: list[HumanEvidenceIntegrityRepairRecord] = Field(
        default_factory=list
    )
    evidence_revision_plan: EvidenceRevisionPlan | None = None
    completed_agents: list[str] = Field(default_factory=list)
    failed_agents: list[str] = Field(default_factory=list)
    revision_count: int = Field(default=0, ge=0)
    revision_history: list[ResearchRevisionRecord] = Field(default_factory=list)
    external_revision_count: int = Field(default=0, ge=0)
    external_revision_history: list[ExternalResearchRevisionRecord] = Field(default_factory=list)
    pending_external_revision_request_ids: list[str] = Field(default_factory=list)
    pending_revision_parent_message_id: str | None = None
    pending_revision_source_agent_id: str | None = None
    external_revision_reply_sent: bool = False
    external_revision_status: ExternalRevisionCheckpoint | None = None
    revision_control: RevisionControlState = Field(default_factory=RevisionControlState)
    message_history: list[PMPMessage] = Field(default_factory=list)
    role_definition_usage: list[dict[str, str]] = Field(default_factory=list)
    deliberation_sent: bool = False
    limitations: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()

    def final_result(self) -> dict[str, Any] | None:
        if self.research_report is None:
            return None
        return {
            "research_report": self.research_report,
            "quality_review": self.review_result,
            "human_evidence_decision": (
                self.human_evidence_decision.model_dump(mode="json")
                if self.human_evidence_decision is not None
                else None
            ),
            "accepted_evidence_gaps": [
                item.model_dump(mode="json") for item in self.accepted_evidence_gaps
            ],
            "deliberation_sent": self.deliberation_sent,
            "external_revision_count": self.external_revision_count,
            "external_revision_reply_sent": self.external_revision_reply_sent,
        }
