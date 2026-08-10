from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from common.models.pmp import PMPMessage
from common.models.workflow import WorkflowStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    target_agent_ids: list[str] = Field(min_length=1)
    findings: list[dict[str, Any]] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


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
    completed_agents: list[str] = Field(default_factory=list)
    failed_agents: list[str] = Field(default_factory=list)
    revision_count: int = Field(default=0, ge=0)
    revision_history: list[ResearchRevisionRecord] = Field(default_factory=list)
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
            "deliberation_sent": self.deliberation_sent,
        }
