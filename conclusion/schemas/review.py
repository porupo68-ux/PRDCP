from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from conclusion.schemas.conclusion_package import ConclusionPackage
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult
from conclusion.schemas.decision_integration import DecisionIntegrationResult
from conclusion.schemas.position_candidate import PositionGenerationResult
from conclusion.schemas.upstream_revision import UpstreamDeliberationRequest
from conclusion.schemas.strict_references import (
    bind_strict_reference_fields,
    candidate_reference_values,
    explicit_reference_values,
    unique_strings,
)


class QualityGateDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class PlaywrightReadiness(str, Enum):
    READY = "ready"
    READY_WITH_CONDITIONS = "ready_with_conditions"
    NOT_READY = "not_ready"
    NOT_APPLICABLE = "not_applicable"


class RevisionScope(str, Enum):
    NONE = "none"
    TARGETED = "targeted"
    MULTI_AGENT = "multi_agent"
    MANAGER_REINTEGRATION = "manager_reintegration"
    DELIBERATION_RETURN = "deliberation_return"


class ConclusionRevisionTarget(str, Enum):
    POSITION_GENERATOR = "conclusion.position_generator"
    DECISION_EVALUATOR = "conclusion.decision_evaluator"
    DECISION_INTEGRATOR = "conclusion.decision_integrator"
    MANAGER = "conclusion.manager"


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

    task_id: str = Field(min_length=1)
    position_generation: PositionGenerationResult
    decision_evaluation: DecisionEvaluationResult
    decision_integration: DecisionIntegrationResult
    conclusion_package: ConclusionPackage
    deterministic_validation: DeterministicValidationResult
    revision_context: dict[str, Any] | None = None


class ConclusionQualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    review_id: str = Field(min_length=1)
    status: QualityGateDecision = Field(
        description=(
            "Exclusive quality-gate decision. approved and approved_with_conditions "
            "must not request a revision; repairable issues use revision_required; "
            "blocked is only for an unreviewable or non-repairable gate."
        )
    )
    reason: str = Field(min_length=1)
    playwright_readiness: PlaywrightReadiness = Field(
        description=(
            "approved=ready, approved_with_conditions=ready_with_conditions, and "
            "revision_required or blocked=not_ready/not_applicable."
        )
    )
    findings: list[ConclusionQualityFinding] = Field(default_factory=list)
    blocking_finding_ids: list[str] = Field(default_factory=list)
    revision_scope: RevisionScope = Field(
        default=RevisionScope.NONE,
        description=(
            "Use none for approvals/blocked, targeted for exactly one internal target, "
            "multi_agent for multiple internal targets, manager_reintegration for an "
            "internal reintegration, or deliberation_return for upstream requests only."
        ),
    )
    revision_targets: list[ConclusionRevisionTarget] = Field(
        default_factory=list,
        description=(
            "Conclusion-internal agents to rerun. Must be empty for approvals, blocked, "
            "and deliberation_return."
        ),
    )
    upstream_revision_requests: list[UpstreamDeliberationRequest] = Field(
        default_factory=list,
        description=(
            "Deliberation return requests. Use only with status=revision_required and "
            "revision_scope=deliberation_return; internal fixes never belong here."
        ),
    )
    limitations_to_disclose: list[str] = Field(
        default_factory=list,
        description=(
            "Disclosure-only conditions that do not require artifact changes. "
            "approved_with_conditions requires at least one entry."
        ),
    )
    reviewed_candidate_ids: list[str] = Field(min_length=1)
    reviewed_evaluation_result_id: str = Field(min_length=1)
    reviewed_integration_result_id: str = Field(min_length=1)

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        candidate_ids = candidate_reference_values(input_data)
        claim_ids = explicit_reference_values(input_data, "claim")
        evaluation = input_data.get("decision_evaluation")
        integration = input_data.get("decision_integration")
        evaluation_id = (
            evaluation.get("decision_evaluation_result_id")
            if isinstance(evaluation, dict)
            else None
        )
        integration_id = (
            integration.get("decision_integration_result_id")
            if isinstance(integration, dict)
            else None
        )
        return bind_strict_reference_fields(
            schema,
            list_fields={
                "affected_candidate_ids": candidate_ids,
                "affected_claim_ids": claim_ids,
                "reviewed_candidate_ids": candidate_ids,
            },
            scalar_fields={
                "reviewed_evaluation_result_id": unique_strings([evaluation_id]),
                "reviewed_integration_result_id": unique_strings([integration_id]),
            },
        )

    @field_validator("playwright_readiness", mode="before")
    @classmethod
    def normalize_legacy_playwright_readiness(cls, value: Any) -> Any:
        """Read legacy uppercase checkpoints without rewriting persisted data."""

        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_routing(self) -> "ConclusionQualityReviewOutput":
        ready_states = {
            PlaywrightReadiness.READY,
            PlaywrightReadiness.READY_WITH_CONDITIONS,
        }
        if self.status in {QualityGateDecision.APPROVED, QualityGateDecision.APPROVED_WITH_CONDITIONS}:
            if self.revision_targets or self.upstream_revision_requests:
                raise ValueError("approved review cannot route revisions")
            if self.revision_scope != RevisionScope.NONE:
                raise ValueError("approved review must use revision_scope=none")
            if self.playwright_readiness not in ready_states:
                raise ValueError("approved review must be Playwright-ready")
            if self.blocking_finding_ids:
                raise ValueError("approved review cannot identify blocking findings")
            if (
                self.status == QualityGateDecision.APPROVED
                and self.playwright_readiness != PlaywrightReadiness.READY
            ):
                raise ValueError("approved review must use playwright_readiness=ready")
            if (
                self.status == QualityGateDecision.APPROVED_WITH_CONDITIONS
                and self.playwright_readiness != PlaywrightReadiness.READY_WITH_CONDITIONS
            ):
                raise ValueError(
                    "approved_with_conditions must use "
                    "playwright_readiness=ready_with_conditions"
                )
            if (
                self.status == QualityGateDecision.APPROVED_WITH_CONDITIONS
                and not self.limitations_to_disclose
            ):
                raise ValueError(
                    "approved_with_conditions must disclose at least one limitation"
                )
        elif self.status == QualityGateDecision.REVISION_REQUIRED:
            if self.playwright_readiness in ready_states:
                raise ValueError("revision_required review cannot be Playwright-ready")
            if not self.findings:
                raise ValueError("revision_required must include findings")
            if not self.revision_targets and not self.upstream_revision_requests:
                raise ValueError("revision_required must route an internal or upstream revision")
            if self.revision_scope == RevisionScope.NONE:
                raise ValueError("revision_required cannot use revision_scope=none")
            if self.revision_scope == RevisionScope.DELIBERATION_RETURN:
                if not self.upstream_revision_requests:
                    raise ValueError("deliberation_return requires upstream revision requests")
                if self.revision_targets:
                    raise ValueError(
                        "deliberation_return cannot mix internal revision targets"
                    )
            else:
                if self.upstream_revision_requests:
                    raise ValueError(
                        "internal revision scope cannot include upstream revision requests"
                    )
                if not self.revision_targets:
                    raise ValueError("internal revision scope requires revision_targets")
                if (
                    self.revision_scope == RevisionScope.TARGETED
                    and len(self.revision_targets) != 1
                ):
                    raise ValueError("targeted revision requires exactly one target")
                if (
                    self.revision_scope == RevisionScope.MULTI_AGENT
                    and len(self.revision_targets) < 2
                ):
                    raise ValueError("multi_agent revision requires multiple targets")
        else:
            if self.playwright_readiness in ready_states:
                raise ValueError("blocked review cannot be Playwright-ready")
            if not self.blocking_finding_ids:
                raise ValueError("blocked review must identify blocking findings")
            if self.revision_scope != RevisionScope.NONE:
                raise ValueError("blocked review must use revision_scope=none")
            if self.revision_targets or self.upstream_revision_requests:
                raise ValueError("blocked review cannot route revisions")

        finding_ids = {finding.finding_id for finding in self.findings}
        if not set(self.blocking_finding_ids).issubset(finding_ids):
            raise ValueError("blocking_finding_ids must reference findings")
        upstream_finding_ids = {
            finding_id
            for request in self.upstream_revision_requests
            for finding_id in request.source_finding_ids
        }
        if not upstream_finding_ids.issubset(finding_ids):
            raise ValueError("upstream source_finding_ids must reference findings")
        return self
