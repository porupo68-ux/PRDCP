from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MessageType(str, Enum):
    TASK = "task"
    RESULT = "result"
    INFO = "info"
    ERROR = "error"
    REVIEW = "review"
    REVISION_REQUEST = "revision_request"
    REVISION_RESULT = "revision_result"
    STATUS_UPDATE = "status_update"
    CANCELLATION = "cancellation"
    RESEARCH_PLAN = "research_plan"
    RESEARCH_RESULT = "research_result"
    RESEARCH_REVISION_REQUEST = "research_revision_request"
    RESEARCH_REVISION_RESULT = "research_revision_result"
    HUMAN_EVIDENCE_DECISION = "human_evidence_decision"
    DELIBERATION_TASK_ASSIGNMENT = "deliberation_task_assignment"
    DELIBERATION_TASK_RESULT = "deliberation_task_result"
    DELIBERATION_RESULT = "deliberation_result"
    DELIBERATION_QUALITY_REVIEW_ASSIGNMENT = "deliberation_quality_review_assignment"
    DELIBERATION_QUALITY_REVIEW_RESULT = "deliberation_quality_review_result"
    POSITION_GENERATION_ASSIGNMENT = "position_generation_assignment"
    POSITION_GENERATION_RESULT = "position_generation_result"
    DECISION_EVALUATION_ASSIGNMENT = "decision_evaluation_assignment"
    DECISION_EVALUATION_RESULT = "decision_evaluation_result"
    DECISION_INTEGRATION_ASSIGNMENT = "decision_integration_assignment"
    DECISION_INTEGRATION_RESULT = "decision_integration_result"
    CONCLUSION_QUALITY_REVIEW_ASSIGNMENT = "conclusion_quality_review_assignment"
    CONCLUSION_QUALITY_REVIEW_RESULT = "conclusion_quality_review_result"
    CONCLUSION_HANDOFF = "conclusion_handoff"
    QUALITY_REVIEW_REQUEST = "quality_review_request"
    QUALITY_REVIEW_RESULT = "quality_review_result"
    FINAL_SCRIPT_DELIVERY = "final_script_delivery"


class MessageStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REVISION_REQUIRED = "revision_required"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PMPContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_stage: str = ""
    previous_stage: str = ""
    next_stage: str = ""


class PMPRouting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_target: str | None = None
    reply_required: bool = True


class PMPMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    status: MessageStatus = MessageStatus.CREATED
    priority: Priority = Priority.MEDIUM
    retry_count: int = Field(default=0, ge=0)
    notes: str = ""
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("PMP timestamps must include a timezone")
        return value.astimezone(timezone.utc)


class PMPMessage(BaseModel):
    """Canonical PMP v2.0 envelope."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    protocol_version: str = Field(default="2.0", pattern=r"^2\.0$")
    message_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    workflow_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    parent_message_id: str | None = None
    sender_agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    receiver_agent_id: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    message_type: MessageType
    objective: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    context: PMPContext = Field(default_factory=PMPContext)
    routing: PMPRouting = Field(default_factory=PMPRouting)
    metadata: PMPMetadata = Field(default_factory=PMPMetadata)

    @field_validator("message_id", "workflow_id")
    @classmethod
    def require_uuid(cls, value: str) -> str:
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("message_id and workflow_id must be UUID strings") from exc
        return value

    @field_validator("parent_message_id")
    @classmethod
    def validate_parent_uuid(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                UUID(value)
            except ValueError as exc:
                raise ValueError("parent_message_id must be a UUID string or null") from exc
        return value

    @model_validator(mode="after")
    def prevent_self_routing(self) -> "PMPMessage":
        if self.sender_agent_id == self.receiver_agent_id:
            raise ValueError("sender_agent_id and receiver_agent_id must differ")
        return self

    @classmethod
    def create(
        cls,
        *,
        sender_agent_id: str,
        receiver_agent_id: str,
        message_type: MessageType | str,
        objective: str,
        payload: dict[str, Any],
        workflow_id: str | None = None,
        parent_message_id: str | None = None,
        constraints: dict[str, Any] | None = None,
        context: PMPContext | dict[str, Any] | None = None,
        routing: PMPRouting | dict[str, Any] | None = None,
        metadata: PMPMetadata | dict[str, Any] | None = None,
    ) -> "PMPMessage":
        return cls(
            workflow_id=workflow_id or str(uuid4()),
            parent_message_id=parent_message_id,
            sender_agent_id=sender_agent_id,
            receiver_agent_id=receiver_agent_id,
            message_type=message_type,
            objective=objective,
            payload=payload,
            constraints=constraints or {},
            context=context or PMPContext(),
            routing=routing or PMPRouting(),
            metadata=metadata or PMPMetadata(),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return self.model_dump_json(indent=indent)

    @classmethod
    def from_json(cls, data: str) -> "PMPMessage":
        return cls.model_validate_json(data)

    def with_retry(self, note: str = "") -> "PMPMessage":
        data = self.model_dump()
        data["message_id"] = str(uuid4())
        data["parent_message_id"] = self.message_id
        data["metadata"]["updated_at"] = utc_now()
        data["metadata"]["retry_count"] += 1
        if note:
            data["metadata"]["notes"] = note
        return PMPMessage.model_validate(data)
