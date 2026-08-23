from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from common.models.pmp import PMPMessage
from common.models.revision import RevisionControlState
from common.models.workflow import WorkflowStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeliberationRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    target_agent_ids: list[str] = Field(min_length=1)
    findings: list[dict[str, Any]] = Field(min_length=1)
    rerun_stages: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class UpstreamRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    request_message_id: str = Field(min_length=1)
    requests: list[dict[str, Any]] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class DeliberationWorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_assignment=True)

    workflow_id: str = Field(min_length=1)
    status: WorkflowStatus = WorkflowStatus.CREATED
    researcher_handoff: dict[str, Any]
    research_report: dict[str, Any]
    analysis_tasks: list[dict[str, Any]] = Field(default_factory=list)
    current_agent_ids: list[str] = Field(default_factory=list)
    analysis_results: dict[str, dict[str, Any]] = Field(default_factory=dict)
    initial_integration: dict[str, Any] | None = None
    counterargument_analysis: dict[str, Any] | None = None
    final_integration: dict[str, Any] | None = None
    deterministic_validation: dict[str, Any] | None = None
    manager_invalid_payloads: dict[str, dict[str, Any]] = Field(default_factory=dict)
    manager_payload_recoveries: list[dict[str, Any]] = Field(default_factory=list)
    counterargument_payload_recoveries: list[dict[str, Any]] = Field(default_factory=list)
    manager_provider_failures: list[dict[str, Any]] = Field(default_factory=list)
    deliberation_result: dict[str, Any] | None = None
    review_result: dict[str, Any] | None = None
    checkpoint_revisions: dict[str, int] = Field(default_factory=dict)
    completed_agents: list[str] = Field(default_factory=list)
    failed_agents: list[str] = Field(default_factory=list)
    revision_count: int = Field(default=0, ge=0)
    revision_history: list[DeliberationRevisionRecord] = Field(default_factory=list)
    upstream_revision_count: int = Field(default=0, ge=0)
    upstream_revision_history: list[UpstreamRevisionRecord] = Field(default_factory=list)
    pending_revision_targets: list[str] = Field(default_factory=list)
    pending_revision_finding_ids: list[str] = Field(default_factory=list)
    pending_upstream_revision_request_ids: list[str] = Field(default_factory=list)
    pending_revision_scope: str | None = None
    pending_revision_iteration: int | None = Field(default=None, ge=1)
    pending_revision_review_id: str | None = None
    awaiting_upstream_revision: bool = False
    revision_control: RevisionControlState = Field(default_factory=RevisionControlState)
    message_history: list[PMPMessage] = Field(default_factory=list)
    role_definition_usage: list[dict[str, str]] = Field(default_factory=list)
    conclusion_sent: bool = False
    limitations: list[dict[str, Any]] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()

    def final_result(self) -> dict[str, Any] | None:
        if self.deliberation_result is None:
            return None
        return {
            "deliberation_result": self.deliberation_result,
            "quality_review": self.review_result,
            "conclusion_sent": self.conclusion_sent,
        }
