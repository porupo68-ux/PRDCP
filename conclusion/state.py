from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from common.models.pmp import PMPMessage
from common.models.revision import RevisionControlState
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


class ConclusionManagerRepairRecord(BaseModel):
    """Audited, deterministic repair of a Manager-owned package artifact."""

    model_config = ConfigDict(extra="forbid")

    iteration: int = Field(ge=1)
    upstream_revision_count: int = Field(ge=0)
    revision_count: int = Field(ge=0)
    source_review_id: str = Field(min_length=1)
    source_finding_ids: list[str] = Field(min_length=1)
    repair_kind: str = Field(pattern=r"^alternative_materialization$")
    added_alternative_candidate_ids: list[str] = Field(min_length=1)
    reviewer_task_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class CandidateCoverageAudit(BaseModel):
    """Persisted Decision Evaluator coverage check for recovery and audit."""

    model_config = ConfigDict(extra="forbid")

    source_task_id: str = Field(min_length=1)
    recovery_task_id: str | None = None
    position_candidate_ids: list[str]
    evaluation_candidate_ids: list[str]
    matrix_candidate_ids: list[str]
    candidate_count_position_generator: int = Field(ge=0)
    candidate_count_evaluation: int = Field(ge=0)
    candidate_count_matrix: int = Field(ge=0)
    candidate_evaluation_row_count: int = Field(ge=0)
    expected_candidate_evaluation_row_count: int = Field(ge=0)
    missing_candidate_ids: list[str]
    extra_candidate_ids: list[str]
    passed: bool
    checked_at: datetime = Field(default_factory=utc_now)


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
    candidate_coverage_checked: bool = False
    candidate_coverage_passed: bool = False
    candidate_coverage_audit: CandidateCoverageAudit | None = None
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
    manager_repair_history: list[ConclusionManagerRepairRecord] = Field(
        default_factory=list
    )
    upstream_revision_count: int = Field(default=0, ge=0)
    upstream_revision_history: list[ConclusionUpstreamRevisionRecord] = Field(default_factory=list)
    revision_control: RevisionControlState = Field(default_factory=RevisionControlState)
    message_history: list[PMPMessage] = Field(default_factory=list)
    role_definition_usage: list[dict[str, str]] = Field(default_factory=list)
    provider_payload_recoveries: list[dict[str, Any]] = Field(default_factory=list)
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
