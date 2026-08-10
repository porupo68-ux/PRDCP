from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.integrated_analysis import FinalIntegratedAnalysis, InitialIntegratedAnalysis


class QualityGateDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class RevisionScope(str, Enum):
    NONE = "none"
    TARGETED = "targeted"
    MULTI_AGENT = "multi_agent"
    MANAGER_REINTEGRATION = "manager_reintegration"
    RESEARCHER_RETURN = "researcher_return"
    FULL_DELIBERATION_RESTART = "full_deliberation_restart"


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


class QualityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    category: str = Field(min_length=1)
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    affected_agent_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class UpstreamResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_request_id: str = Field(min_length=1)
    research_question_id: str = Field(min_length=1)
    affected_claim_ids: list[str] = Field(default_factory=list)
    missing_evidence_description: str = Field(min_length=1)
    preferred_source_categories: list[str] = Field(min_length=1)
    required_scope: dict[str, Any]
    acceptance_conditions: list[str] = Field(min_length=1)
    requesting_agent_id: str = Field(min_length=1)
    source_finding_ids: list[str] = Field(min_length=1)


class DeliberationQualityReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_report: dict[str, Any]
    primary_analyses: dict[str, dict[str, Any]]
    initial_integration: InitialIntegratedAnalysis
    counterargument_analysis: CounterargumentAnalysisResult
    final_integration: FinalIntegratedAnalysis
    deterministic_validation: DeterministicValidationResult
    failed_agent_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    revision_context: dict[str, Any] | None = None


class DeliberationQualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    review_id: str = Field(min_length=1)
    status: QualityGateDecision
    conclusion_readiness: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    findings: list[QualityFinding] = Field(default_factory=list)
    blocking_finding_ids: list[str] = Field(default_factory=list)
    revision_scope: RevisionScope = RevisionScope.NONE
    revision_targets: list[str] = Field(default_factory=list)
    upstream_revision_requests: list[UpstreamResearchRequest] = Field(default_factory=list)
    limitations_to_disclose: list[str] = Field(default_factory=list)
    reviewed_analysis_ids: list[str] = Field(min_length=1)
    reviewed_evidence_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_decision(self) -> "DeliberationQualityReviewOutput":
        allowed_targets = {
            "deliberation.argument_analyst",
            "deliberation.causal_structural_analyst",
            "deliberation.stakeholder_response_analyst",
            "deliberation.counterargument_analyst",
            "deliberation.manager",
        }
        invalid = set(self.revision_targets) - allowed_targets
        if invalid:
            raise ValueError(f"invalid Deliberation revision targets: {sorted(invalid)}")
        if self.status in {
            QualityGateDecision.APPROVED,
            QualityGateDecision.APPROVED_WITH_CONDITIONS,
        }:
            if self.revision_targets or self.upstream_revision_requests:
                raise ValueError("approved review cannot route revisions")
            if self.revision_scope != RevisionScope.NONE:
                raise ValueError("approved review must use revision_scope=none")
        elif self.status == QualityGateDecision.REVISION_REQUIRED:
            if not self.findings:
                raise ValueError("revision_required must include findings")
            if not self.revision_targets and not self.upstream_revision_requests:
                raise ValueError("revision_required must route an internal or upstream revision")
            if self.revision_scope == RevisionScope.RESEARCHER_RETURN and not self.upstream_revision_requests:
                raise ValueError("researcher_return requires upstream_revision_requests")
        elif not self.blocking_finding_ids:
            raise ValueError("blocked review must identify blocking findings")
        return self
