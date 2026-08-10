from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from common.models.pmp import PMPMessage
from common.models.workflow import WorkflowStatus


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    target_agent: str
    reason: str
    required_action: str
    created_at: datetime = Field(default_factory=utc_now)


class ProducerWorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_assignment=True)

    workflow_id: str
    status: WorkflowStatus = WorkflowStatus.CREATED
    current_agent_id: str | None = None
    initial_request: dict[str, Any]
    topic_candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected_topic: dict[str, Any] | None = None
    general_opinion: dict[str, Any] | None = None
    research_plan: dict[str, Any] | None = None
    review_result: dict[str, Any] | None = None
    revision_count: int = Field(default=0, ge=0)
    revision_history: list[RevisionRecord] = Field(default_factory=list)
    completed_agents: list[str] = Field(default_factory=list)
    message_history: list[PMPMessage] = Field(default_factory=list)
    role_definition_usage: list[dict[str, str]] = Field(default_factory=list)
    researcher_sent: bool = False
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()

    def final_result(self) -> dict[str, Any] | None:
        if not all([self.selected_topic, self.general_opinion, self.research_plan, self.review_result]):
            return None
        return {
            "topic": self.selected_topic,
            "general_opinion": self.general_opinion,
            "research_plan": self.research_plan,
            "review": self.review_result,
        }
