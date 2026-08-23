from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class LayerId(str, Enum):
    PRODUCER = "producer"
    RESEARCHER = "researcher"
    DELIBERATION = "deliberation"
    CONCLUSION = "conclusion"
    PLAYWRIGHT = "playwright"


UPSTREAM_LAYER: dict[LayerId, LayerId] = {
    LayerId.RESEARCHER: LayerId.PRODUCER,
    LayerId.DELIBERATION: LayerId.RESEARCHER,
    LayerId.CONCLUSION: LayerId.DELIBERATION,
    LayerId.PLAYWRIGHT: LayerId.CONCLUSION,
}


class RevisionRoute(str, Enum):
    INTERNAL = "internal"
    UPSTREAM = "upstream"


class RevisionExecutionStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    FAILED = "failed"


class RevisionFindingOutcome(str, Enum):
    RESOLVED = "resolved"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"
    ROUTED_UPSTREAM = "routed_upstream"


class HumanSelectionImpact(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    UNCHANGED = "unchanged"
    RESELECTION_REQUIRED = "reselection_required"


class RevisionControlPhase(str, Enum):
    IDLE = "idle"
    PLANNED = "planned"
    AUTHORIZATION_REQUIRED = "authorization_required"
    REQUESTED = "requested"
    WAITING_UPSTREAM_RESULT = "waiting_upstream_result"
    CONSUMING_REQUEST = "consuming_request"
    EXECUTING = "executing"
    RESULT_READY = "result_ready"
    COMPLETED = "completed"
    BLOCKED = "blocked"


class RevisionAuthorizationStatus(str, Enum):
    PENDING = "pending"
    CONSUMED = "consumed"


class RevisionAuditEventType(str, Enum):
    REQUEST_WRITTEN = "request_written"
    REQUEST_CONSUMED = "request_consumed"
    AUTHORIZATION_CREATED = "authorization_created"
    AUTHORIZATION_CONSUMED = "authorization_consumed"
    BUDGET_CONSUMED = "budget_consumed"
    PROVIDER_RESERVED = "provider_reserved"
    RETRIEVAL_RESERVED = "retrieval_reserved"
    RESULT_WRITTEN = "result_written"
    RESULT_CONSUMED = "result_consumed"
    BLOCKED = "blocked"


class RevisionArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_type: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    artifact_id: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: int = Field(default=1, ge=1)


class RevisionFindingDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    finding_id: str = Field(min_length=1)
    outcome: RevisionFindingOutcome
    reason: str = Field(min_length=1)
    result_artifact_ids: list[str] = Field(default_factory=list)
    child_revision_request_id: str | None = None

    @model_validator(mode="after")
    def require_child_for_upstream(self) -> "RevisionFindingDisposition":
        if (
            self.outcome == RevisionFindingOutcome.ROUTED_UPSTREAM.value
            and not self.child_revision_request_id
        ):
            raise ValueError("routed_upstream disposition requires child_revision_request_id")
        if (
            self.outcome != RevisionFindingOutcome.ROUTED_UPSTREAM.value
            and self.child_revision_request_id is not None
        ):
            raise ValueError("child_revision_request_id is valid only for routed_upstream")
        return self


class RevisionRequestV1(BaseModel):
    """Canonical request payload for both internal and adjacent-layer revision."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: Literal["revision.v1"] = "revision.v1"
    revision_request_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    route: RevisionRoute
    source_layer: LayerId
    target_layer: LayerId
    revision_epoch: int = Field(ge=1)
    root_revision_request_id: str = Field(min_length=1)
    parent_revision_request_id: str | None = None
    source_review_id: str = Field(min_length=1)
    source_finding_ids: list[str] = Field(min_length=1)
    target_agent_ids: list[str] = Field(min_length=1)
    base_artifacts: list[RevisionArtifactRef] = Field(min_length=1)
    required_actions: list[str] = Field(min_length=1)
    acceptance_conditions: list[str] = Field(min_length=1)
    evidence_expansion_allowed: bool = False
    retrieval_allowed: bool = False
    expected_human_selection_impact: HumanSelectionImpact = (
        HumanSelectionImpact.NOT_APPLICABLE
    )
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def create(cls, **data: Any) -> "RevisionRequestV1":
        initial = cls.model_validate({**data, "idempotency_key": "0" * 64})
        return initial.model_copy(
            update={
                "idempotency_key": revision_idempotency_key(
                    initial.model_dump(mode="json")
                )
            }
        )

    @field_validator("workflow_id")
    @classmethod
    def validate_workflow_uuid(cls, value: str) -> str:
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("workflow_id must be a UUID string") from exc
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_route_and_identity(self) -> "RevisionRequestV1":
        _require_unique(self.source_finding_ids, "source_finding_ids")
        _require_unique(self.target_agent_ids, "target_agent_ids")
        _require_unique(self.required_actions, "required_actions")
        _require_unique(self.acceptance_conditions, "acceptance_conditions")
        artifact_keys = [
            f"{item.artifact_type}:{item.artifact_id}" for item in self.base_artifacts
        ]
        _require_unique(artifact_keys, "base_artifacts")

        source = LayerId(self.source_layer)
        target = LayerId(self.target_layer)
        route = RevisionRoute(self.route)
        if route is RevisionRoute.INTERNAL:
            if source is not target:
                raise ValueError("internal revision must remain in its source layer")
        else:
            if source is LayerId.PRODUCER:
                raise ValueError("Producer cannot request an upstream revision")
            if UPSTREAM_LAYER.get(source) is not target:
                raise ValueError("upstream revision must target the immediately preceding layer")

        for agent_id in self.target_agent_ids:
            if not agent_id.startswith(f"{target.value}."):
                raise ValueError("target_agent_ids must belong to target_layer")
        if self.retrieval_allowed and not self.evidence_expansion_allowed:
            raise ValueError("retrieval_allowed requires evidence_expansion_allowed")
        if self.retrieval_allowed and target not in {
            LayerId.PRODUCER,
            LayerId.RESEARCHER,
        }:
            raise ValueError(
                "Retrieval is permitted only for Producer discovery or Researcher evidence expansion"
            )
        if self.parent_revision_request_id == self.revision_request_id:
            raise ValueError("A revision request cannot be its own parent")
        if (
            self.parent_revision_request_id is None
            and self.root_revision_request_id != self.revision_request_id
        ):
            raise ValueError("A root request must identify itself as root_revision_request_id")
        if self.parent_revision_request_id and not self.root_revision_request_id:
            raise ValueError("A child revision request must preserve root_revision_request_id")
        return self


class RevisionResultV1(BaseModel):
    """Canonical result payload returned to the immediate requesting layer."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_version: Literal["revision.v1"] = "revision.v1"
    revision_result_id: str = Field(min_length=1)
    revision_request_id: str = Field(min_length=1)
    request_message_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    requester_layer: LayerId
    producer_layer: LayerId
    revision_epoch: int = Field(ge=1)
    status: RevisionExecutionStatus
    base_artifacts: list[RevisionArtifactRef] = Field(min_length=1)
    result_artifacts: list[RevisionArtifactRef] = Field(default_factory=list)
    finding_dispositions: list[RevisionFindingDisposition] = Field(min_length=1)
    human_selection_impact: HumanSelectionImpact = HumanSelectionImpact.NOT_APPLICABLE
    provider_reservation_ids: list[str] = Field(default_factory=list)
    retrieval_reservation_ids: list[str] = Field(default_factory=list)
    provider_call_count: int = Field(default=0, ge=0)
    retrieval_call_count: int = Field(default=0, ge=0)
    idempotency_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    completed_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def create(cls, **data: Any) -> "RevisionResultV1":
        initial = cls.model_validate({**data, "idempotency_key": "0" * 64})
        return initial.model_copy(
            update={
                "idempotency_key": revision_idempotency_key(
                    initial.model_dump(mode="json")
                )
            }
        )

    @field_validator("workflow_id", "request_message_id")
    @classmethod
    def validate_uuid_fields(cls, value: str) -> str:
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("workflow_id and request_message_id must be UUID strings") from exc
        return value

    @field_validator("completed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("completed_at must include a timezone")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_result(self) -> "RevisionResultV1":
        _require_unique(
            [f"{item.artifact_type}:{item.artifact_id}" for item in self.base_artifacts],
            "base_artifacts",
        )
        _require_unique(
            [f"{item.artifact_type}:{item.artifact_id}" for item in self.result_artifacts],
            "result_artifacts",
        )
        _require_unique(
            [item.finding_id for item in self.finding_dispositions],
            "finding_dispositions",
        )
        _require_unique(self.provider_reservation_ids, "provider_reservation_ids")
        _require_unique(self.retrieval_reservation_ids, "retrieval_reservation_ids")
        if self.provider_call_count > len(self.provider_reservation_ids):
            raise ValueError("provider_call_count exceeds recorded reservations")
        if self.retrieval_call_count > len(self.retrieval_reservation_ids):
            raise ValueError("retrieval_call_count exceeds recorded reservations")
        if self.status == RevisionExecutionStatus.COMPLETED.value:
            unresolved = {
                RevisionFindingOutcome.UNRESOLVED.value,
                RevisionFindingOutcome.ROUTED_UPSTREAM.value,
            }
            if any(item.outcome in unresolved for item in self.finding_dispositions):
                raise ValueError("completed result cannot retain unresolved findings")
            if not self.result_artifacts:
                raise ValueError("completed result requires result_artifacts")
        requester = LayerId(self.requester_layer)
        producer = LayerId(self.producer_layer)
        if UPSTREAM_LAYER.get(requester) is not producer and requester is not producer:
            raise ValueError("revision result layers are not an internal or adjacent route")
        if requester is not LayerId.PLAYWRIGHT:
            if self.human_selection_impact != HumanSelectionImpact.NOT_APPLICABLE.value:
                raise ValueError(
                    "human_selection_impact applies only to a result returned to Playwright"
                )
        elif producer is LayerId.CONCLUSION:
            if self.human_selection_impact == HumanSelectionImpact.NOT_APPLICABLE.value:
                raise ValueError(
                    "Conclusion result returned to Playwright must declare selection impact"
                )
        return self


class RevisionControlState(BaseModel):
    """Additive state embedded in every Layer without replacing legacy fields."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True, validate_assignment=True)

    schema_version: Literal["revision.v1"] = "revision.v1"
    phase: RevisionControlPhase = RevisionControlPhase.IDLE
    revision_epoch: int = Field(default=0, ge=0)
    active_request_id: str | None = None
    active_request_message_id: str | None = None
    active_result_id: str | None = None
    root_revision_request_id: str | None = None
    parent_revision_request_id: str | None = None
    pending_request_ids: list[str] = Field(default_factory=list)
    consumed_request_ids: list[str] = Field(default_factory=list)
    consumed_result_ids: list[str] = Field(default_factory=list)
    audit_event_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_control_state(self) -> "RevisionControlState":
        for name in (
            "pending_request_ids",
            "consumed_request_ids",
            "consumed_result_ids",
            "audit_event_ids",
        ):
            _require_unique(getattr(self, name), name)
        if set(self.pending_request_ids) & set(self.consumed_request_ids):
            raise ValueError("A request cannot be both pending and consumed")
        if self.revision_epoch == 0 and self.phase != RevisionControlPhase.IDLE.value:
            raise ValueError("non-idle revision control requires revision_epoch >= 1")
        return self


class RevisionBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_limit: int = Field(ge=0)
    upstream_limit: int = Field(ge=0)

    def limit_for(self, route: RevisionRoute | str) -> int:
        return (
            self.internal_limit
            if RevisionRoute(route) is RevisionRoute.INTERNAL
            else self.upstream_limit
        )


class RevisionBudgetConsumption(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    workflow_id: str
    layer: LayerId
    route: RevisionRoute
    revision_request_id: str = Field(min_length=1)
    iteration: int = Field(ge=1)
    consumed_at: datetime = Field(default_factory=utc_now)


class RevisionExecutionAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    authorization_id: str = Field(min_length=1)
    workflow_id: str
    revision_request_id: str = Field(min_length=1)
    executing_layer: LayerId
    actor_id: str = Field(min_length=1)
    actor_source: Literal["CLI", "DISCORD", "API", "SYSTEM"]
    reason: str = Field(min_length=1)
    max_provider_calls: int = Field(default=0, ge=0)
    max_retrieval_calls: int = Field(default=0, ge=0)
    status: RevisionAuthorizationStatus = RevisionAuthorizationStatus.PENDING
    created_at: datetime = Field(default_factory=utc_now)
    consumed_at: datetime | None = None
    provider_reservation_ids: list[str] = Field(default_factory=list)
    retrieval_reservation_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_authorization(self) -> "RevisionExecutionAuthorization":
        _require_unique(self.provider_reservation_ids, "provider_reservation_ids")
        _require_unique(self.retrieval_reservation_ids, "retrieval_reservation_ids")
        if len(self.provider_reservation_ids) > self.max_provider_calls:
            raise ValueError("provider reservations exceed authorization")
        if len(self.retrieval_reservation_ids) > self.max_retrieval_calls:
            raise ValueError("retrieval reservations exceed authorization")
        if self.status == RevisionAuthorizationStatus.PENDING.value:
            if self.consumed_at is not None:
                raise ValueError("pending authorization cannot have consumed_at")
            if self.provider_reservation_ids or self.retrieval_reservation_ids:
                raise ValueError("pending authorization cannot claim reservations")
        else:
            if self.consumed_at is None:
                raise ValueError("consumed authorization requires consumed_at")
            if self.max_provider_calls and not self.provider_reservation_ids:
                raise ValueError("consumed Provider authorization requires a reservation")
            if self.max_retrieval_calls and not self.retrieval_reservation_ids:
                raise ValueError("consumed Retrieval authorization requires a reservation")
        return self


class RevisionAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    audit_event_id: str = Field(min_length=1)
    workflow_id: str
    revision_request_id: str = Field(min_length=1)
    layer: LayerId
    event_type: RevisionAuditEventType
    actor_id: str = Field(min_length=1)
    message_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    reservation_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_event(self) -> "RevisionAuditEvent":
        _require_unique(self.artifact_ids, "artifact_ids")
        _require_unique(self.reservation_ids, "reservation_ids")
        return self


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def deterministic_revision_request_id(
    *,
    workflow_id: str,
    source_layer: LayerId | str,
    target_layer: LayerId | str,
    revision_epoch: int,
    source_review_id: str,
    source_finding_ids: list[str],
) -> str:
    UUID(workflow_id)
    identity = "|".join(
        (
            workflow_id,
            LayerId(source_layer).value,
            LayerId(target_layer).value,
            str(revision_epoch),
            source_review_id,
            ",".join(sorted(source_finding_ids)),
        )
    )
    return f"revision_request_{uuid5(NAMESPACE_URL, identity).hex}"


def revision_idempotency_key(payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized.pop("idempotency_key", None)
    normalized.pop("created_at", None)
    normalized.pop("completed_at", None)
    return canonical_sha256(normalized)


def _require_unique(values: list[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must contain unique values")


def safe_path_component(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        return value
    return f"id-{hashlib.sha256(value.encode('utf-8')).hexdigest()}"
