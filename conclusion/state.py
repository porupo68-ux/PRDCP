from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from common.models.pmp import PMPMessage
from common.models.workflow import WorkflowStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ConclusionRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    target_agent_ids: list[str] = Field(min_length=1)
    findings: list[dict[str, Any]] = Field(min_length=1)
    rerun_stages: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class ConclusionUpstreamRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    request_message_id: str = Field(min_length=1)
    requests: list[dict[str, Any]] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class ConclusionWorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_assignment=True)

    workflow_id: str = Field(min_length=1)
    status: WorkflowStatus = WorkflowStatus.CREATED
    deliberation_handoff: dict[str, Any]
    deliberation_result: dict[str, Any]
    decision_context: dict[str, Any] | None = None
    position_generation: dict[str, Any] | None = None
    position_candidates: list[dict[str, Any]] = Field(default_factory=list)
    evaluation_framework: dict[str, Any] | None = None
    decision_evaluation: dict[str, Any] | None = None
    decision_integration: dict[str, Any] | None = None
    conclusion_package: dict[str, Any] | None = None
    deterministic_validation: dict[str, Any] | None = None
    review_result: dict[str, Any] | None = None
    human_selection: dict[str, Any] | None = None
    final_conclusion: dict[str, Any] | None = None
    completed_agents: list[str] = Field(default_factory=list)
    failed_agents: list[str] = Field(default_factory=list)
    current_agent_ids: list[str] = Field(default_factory=list)
    revision_count: int = Field(default=0, ge=0)
    revision_history: list[ConclusionRevisionRecord] = Field(default_factory=list)
    upstream_revision_count: int = Field(default=0, ge=0)
    upstream_revision_history: list[ConclusionUpstreamRevisionRecord] = Field(default_factory=list)
    message_history: list[PMPMessage] = Field(default_factory=list)
    role_definition_usage: list[dict[str, str]] = Field(default_factory=list)
    playwright_sent: bool = False
    limitations: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()

    def final_result(self) -> dict[str, Any] | None:
        if self.final_conclusion is None:
            return None
        return {
            "final_conclusion": self.final_conclusion,
            "human_selection": self.human_selection,
            "playwright_sent": self.playwright_sent,
        }
