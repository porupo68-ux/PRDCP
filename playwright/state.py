from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from common.models.pmp import PMPMessage


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PlaywrightStatus(str, Enum):
    CREATED = "CREATED"
    VALIDATING_HANDOFF = "VALIDATING_HANDOFF"
    DESIGNING_NARRATIVE = "DESIGNING_NARRATIVE"
    WRITING_SCRIPT = "WRITING_SCRIPT"
    EDITING_CITATIONS = "EDITING_CITATIONS"
    DESIGNING_VISUALS = "DESIGNING_VISUALS"
    VALIDATING_PACKAGE = "VALIDATING_PACKAGE"
    REVISING = "REVISING"
    WAITING_UPSTREAM_REVISION = "WAITING_UPSTREAM_REVISION"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class PlaywrightRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    target_agent_ids: list[str] = Field(min_length=1)
    findings: list[dict[str, Any]] = Field(min_length=1)
    rerun_stages: list[str] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class PlaywrightUpstreamRevisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    request_message_id: str = Field(min_length=1)
    requests: list[dict[str, Any]] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class PlaywrightWorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_assignment=True)

    workflow_id: str = Field(min_length=1)
    status: PlaywrightStatus = PlaywrightStatus.CREATED
    conclusion_handoff: dict[str, Any]
    final_conclusion: dict[str, Any]
    conclusion_package: dict[str, Any]
    human_selection: dict[str, Any]
    traceability_manifest: dict[str, Any]
    final_conclusion_hash: str = Field(min_length=1)
    production_context: dict[str, Any] | None = None
    narrative_blueprint: dict[str, Any] | None = None
    script_draft: dict[str, Any] | None = None
    citation_validated_script: dict[str, Any] | None = None
    citation_manifest: dict[str, Any] | None = None
    visual_plan: dict[str, Any] | None = None
    final_script_package: dict[str, Any] | None = None
    deterministic_validation: dict[str, Any] | None = None
    final_gate_result: dict[str, Any] | None = None
    completed_agents: list[str] = Field(default_factory=list)
    failed_agents: list[str] = Field(default_factory=list)
    current_agent_ids: list[str] = Field(default_factory=list)
    revision_count: int = Field(default=0, ge=0)
    revision_history: list[PlaywrightRevisionRecord] = Field(default_factory=list)
    upstream_revision_count: int = Field(default=0, ge=0)
    upstream_revision_history: list[PlaywrightUpstreamRevisionRecord] = Field(default_factory=list)
    message_history: list[PMPMessage] = Field(default_factory=list)
    role_definition_usage: list[dict[str, str]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    delivery_paths: dict[str, str] = Field(default_factory=dict)
    delivered: bool = False
    error: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = utc_now()

    def final_result(self) -> dict[str, Any] | None:
        if self.final_script_package is None:
            return None
        return {
            "final_script_package": self.final_script_package,
            "delivery_paths": self.delivery_paths,
            "delivered": self.delivered,
        }

