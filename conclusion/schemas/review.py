from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conclusion.schemas.conclusion_package import ConclusionPackage
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult
from conclusion.schemas.decision_integration import DecisionIntegrationResult
from conclusion.schemas.position_candidate import PositionGenerationResult
from conclusion.schemas.upstream_revision import UpstreamDeliberationRequest


class QualityGateDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class PlaywrightReadiness(str, Enum):
    READY = "READY"
    READY_WITH_CONDITIONS = "READY_WITH_CONDITIONS"
    NOT_READY = "NOT_READY"


class RevisionScope(str, Enum):
    NONE = "none"
    TARGETED = "targeted"
    MULTI_AGENT = "multi_agent"
    MANAGER_REINTEGRATION = "manager_reintegration"
    DELIBERATION_RETURN = "deliberation_return"


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    category: str = Field(min_length=1)
    message: str = Field(min_length=1)
    affected_ids: list[str] = Field(default_factory=list)


class DeterministicValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    findings: list[ValidationFinding] = Field(default_factory=list)
    metrics: dict[str, int | float] = Field(default_factory=dict)


class ConclusionQualityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    category: str = Field(min_length=1)
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    affected_agent_ids: list[str] = Field(default_factory=list)
    affected_candidate_ids: list[str] = Field(default_factory=list)


class ConclusionQualityReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_generation: PositionGenerationResult
    decision_evaluation: DecisionEvaluationResult
    decision_integration: DecisionIntegrationResult
    conclusion_package: ConclusionPackage
    deterministic_validation: DeterministicValidationResult
    revision_context: dict[str, Any] | None = None


class ConclusionQualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    review_id: str = Field(min_length=1)
    status: QualityGateDecision
    reason: str = Field(min_length=1)
    playwright_readiness: PlaywrightReadiness
    findings: list[ConclusionQualityFinding] = Field(default_factory=list)
    blocking_finding_ids: list[str] = Field(default_factory=list)
    revision_scope: RevisionScope = RevisionScope.NONE
    revision_targets: list[str] = Field(default_factory=list)
    upstream_revision_requests: list[UpstreamDeliberationRequest] = Field(default_factory=list)
    limitations_to_disclose: list[str] = Field(default_factory=list)
    reviewed_candidate_ids: list[str] = Field(min_length=1)
    reviewed_evaluation_result_id: str = Field(min_length=1)
    reviewed_integration_result_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid_routing(self) -> "ConclusionQualityReviewOutput":
        if self.status in {QualityGateDecision.APPROVED, QualityGateDecision.APPROVED_WITH_CONDITIONS}:
            if self.revision_targets or self.upstream_revision_requests:
                raise ValueError("approved review cannot route revisions")
            if self.revision_scope != RevisionScope.NONE:
                raise ValueError("approved review must use revision_scope=none")
            if self.playwright_readiness == PlaywrightReadiness.NOT_READY:
                raise ValueError("approved review must be Playwright-ready")
        elif self.status == QualityGateDecision.REVISION_REQUIRED:
            if not self.findings:
                raise ValueError("revision_required must include findings")
            if not self.revision_targets and not self.upstream_revision_requests:
                raise ValueError("revision_required must route an internal or upstream revision")
            if self.revision_scope == RevisionScope.DELIBERATION_RETURN and not self.upstream_revision_requests:
                raise ValueError("deliberation_return requires upstream revision requests")
        elif not self.blocking_finding_ids:
            raise ValueError("blocked review must identify blocking findings")
        return self
