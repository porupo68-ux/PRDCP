from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.integrated_analysis import FinalIntegratedAnalysis, InitialIntegratedAnalysis
from deliberation.schemas.research_context import DeliberationResearchContext


class QualityGateDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


class ConclusionReadiness(str, Enum):
    """Canonical Deliberation -> Conclusion readiness contract from the RD."""

    READY = "ready"
    READY_WITH_CONDITIONS = "ready_with_conditions"
    NOT_READY = "not_ready"
    UNDETERMINED = "undetermined"


class RevisionScope(str, Enum):
    NONE = "none"
    TARGETED = "targeted"
    MULTI_AGENT = "multi_agent"
    MANAGER_REINTEGRATION = "manager_reintegration"
    RESEARCHER_RETURN = "researcher_return"
    FULL_DELIBERATION_RESTART = "full_deliberation_restart"


class DeliberationRevisionTarget(str, Enum):
    ARGUMENT_ANALYST = "deliberation.argument_analyst"
    CAUSAL_STRUCTURAL_ANALYST = "deliberation.causal_structural_analyst"
    STAKEHOLDER_RESPONSE_ANALYST = "deliberation.stakeholder_response_analyst"
    COUNTERARGUMENT_ANALYST = "deliberation.counterargument_analyst"
    MANAGER = "deliberation.manager"


class UpstreamRevisionTarget(str, Enum):
    RESEARCHER_MANAGER = "researcher.manager"


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    category: str = Field(min_length=1)
    message: str = Field(min_length=1)
    affected_ids: list[str] = Field(default_factory=list)


class ValidationMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_analysis_count: int = Field(ge=0)
    analysis_id_count: int = Field(ge=0)
    task_id_count: int = Field(ge=0)
    integration_id_count: int = Field(ge=0)
    claim_count: int = Field(ge=0)
    viewpoint_count: int = Field(ge=0)
    evidence_reference_count: int = Field(ge=0)
    report_evidence_count: int = Field(ge=0)
    source_count: int = Field(ge=0)
    report_source_count: int = Field(ge=0)
    counterargument_count: int = Field(ge=0)
    required_revision_count: int = Field(ge=0)
    integration_change_count: int = Field(ge=0)
    unresolved_count: int = Field(ge=0)
    uncertainty_count: int = Field(ge=0)
    workflow_revision_count: int = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_metrics(cls, value: object) -> object:
        if not isinstance(value, dict) or "task_id_count" in value:
            return value
        legacy = value
        return {
            "primary_analysis_count": int(legacy.get("primary_analysis_count", 0)),
            "analysis_id_count": int(legacy.get("analysis_id_count", 0)),
            "task_id_count": 0,
            "integration_id_count": 0,
            "claim_count": int(legacy.get("claim_id_count", 0)),
            "viewpoint_count": int(legacy.get("viewpoint_count", 0)),
            "evidence_reference_count": int(
                legacy.get("referenced_evidence_count", 0)
            ),
            "report_evidence_count": int(legacy.get("evidence_id_count", 0)),
            "source_count": int(legacy.get("source_id_count", 0)),
            "report_source_count": int(legacy.get("source_id_count", 0)),
            "counterargument_count": 0,
            "required_revision_count": 0,
            "integration_change_count": 0,
            "unresolved_count": 0,
            "uncertainty_count": 0,
            "workflow_revision_count": int(legacy.get("revision_count", 0)),
        }


class ValidationTargetSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    integration_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    viewpoint_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    counterargument_ids: list[str] = Field(default_factory=list)
    required_revision_ids: list[str] = Field(default_factory=list)
    integration_change_ids: list[str] = Field(default_factory=list)
    unresolved_ids: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class DeterministicValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "2.0"
    passed: bool
    findings: list[ValidationFinding] = Field(default_factory=list)
    metrics: ValidationMetrics
    validation_targets: ValidationTargetSet = Field(default_factory=ValidationTargetSet)

    @model_validator(mode="before")
    @classmethod
    def mark_legacy_payload(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "schema_version" not in normalized and "validation_targets" not in normalized:
            normalized["schema_version"] = "1.0"
            normalized["validation_targets"] = {}
        return normalized

    @model_validator(mode="after")
    def cross_check_metrics_and_decision(self) -> "DeterministicValidationResult":
        critical = {"ERROR", "CRITICAL"}
        has_blocking_finding = any(
            finding.severity.upper() in critical for finding in self.findings
        )
        if self.passed == has_blocking_finding:
            raise ValueError(
                "passed must be true exactly when no ERROR/CRITICAL finding exists"
            )
        if self.schema_version != "2.0":
            return self
        expected = {
            "analysis_id_count": len(self.validation_targets.analysis_ids),
            "task_id_count": len(self.validation_targets.task_ids),
            "integration_id_count": len(self.validation_targets.integration_ids),
            "claim_count": len(self.validation_targets.claim_ids),
            "viewpoint_count": len(self.validation_targets.viewpoint_ids),
            "evidence_reference_count": len(self.validation_targets.evidence_ids),
            "source_count": len(self.validation_targets.source_ids),
            "counterargument_count": len(
                self.validation_targets.counterargument_ids
            ),
            "required_revision_count": len(
                self.validation_targets.required_revision_ids
            ),
            "integration_change_count": len(
                self.validation_targets.integration_change_ids
            ),
            "unresolved_count": len(self.validation_targets.unresolved_ids),
            "uncertainty_count": len(self.validation_targets.uncertainties),
        }
        mismatches = {
            name: (getattr(self.metrics, name), count)
            for name, count in expected.items()
            if getattr(self.metrics, name) != count
        }
        if mismatches:
            raise ValueError(
                f"validation metrics do not match validation_targets: {mismatches}"
            )
        return self


class QualityFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    category: str = Field(min_length=1)
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    affected_agent_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class RequiredResearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_scope: list[str] = Field(default_factory=list)


class UpstreamResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    revision_request_id: str = Field(min_length=1)
    target_agent_id: UpstreamRevisionTarget = UpstreamRevisionTarget.RESEARCHER_MANAGER
    research_question_id: str = Field(min_length=1)
    affected_claim_ids: list[str] = Field(default_factory=list)
    missing_evidence_description: str = Field(min_length=1)
    preferred_source_categories: list[str] = Field(min_length=1)
    required_scope: RequiredResearchScope
    acceptance_conditions: list[str] = Field(min_length=1)
    requesting_agent_id: str = Field(min_length=1)
    source_finding_ids: list[str] = Field(min_length=1)


class PMPRoutingTraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    parent_message_id: str | None
    sender_agent_id: str = Field(min_length=1)
    receiver_agent_id: str = Field(min_length=1)
    message_type: str = Field(min_length=1)
    status: str = Field(min_length=1)
    revision_target: str | None
    retry_count: int = Field(ge=0)
    execution_order: int = Field(ge=1)
    stage: str = Field(min_length=1)


class CheckpointTraceEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_order: int = Field(ge=1)
    stage: str = Field(min_length=1)
    status: str = Field(min_length=1)
    artifact_ids: list[str] = Field(default_factory=list)
    source_message_ids: list[str] = Field(default_factory=list)
    revision_iteration: int = Field(ge=0)


class DeliberationQualityReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    research_report: DeliberationResearchContext
    primary_analyses: dict[str, dict[str, Any]]
    initial_integration: InitialIntegratedAnalysis
    counterargument_analysis: CounterargumentAnalysisResult
    final_integration: FinalIntegratedAnalysis
    deterministic_validation: DeterministicValidationResult
    pmp_routing_trace: list[PMPRoutingTraceEntry] = Field(min_length=1)
    checkpoint_trace: list[CheckpointTraceEntry] = Field(min_length=1)
    failed_agent_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    revision_context: dict[str, Any] | None = None


class DeliberationQualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    review_id: str = Field(min_length=1)
    status: QualityGateDecision
    conclusion_readiness: ConclusionReadiness
    reason: str = Field(min_length=1)
    findings: list[QualityFinding] = Field(default_factory=list)
    blocking_finding_ids: list[str] = Field(default_factory=list)
    revision_scope: RevisionScope = RevisionScope.NONE
    revision_targets: list[DeliberationRevisionTarget] = Field(default_factory=list)
    upstream_revision_requests: list[UpstreamResearchRequest] = Field(default_factory=list)
    limitations_to_disclose: list[str] = Field(default_factory=list)
    reviewed_analysis_ids: list[str] = Field(min_length=1)
    reviewed_evidence_ids: list[str] = Field(min_length=1)

    @field_validator("conclusion_readiness", mode="before")
    @classmethod
    def normalize_legacy_conclusion_readiness(cls, value: Any) -> Any:
        """Read legacy uppercase checkpoints without changing the stored JSON."""

        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_upstream_target(cls, value: Any) -> Any:
        """Keep Researcher routing out of the internal revision target list."""

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        targets = list(normalized.get("revision_targets") or [])
        if "researcher.manager" not in targets:
            return normalized
        upstream_requests = [dict(item) for item in normalized.get("upstream_revision_requests") or []]
        if normalized.get("revision_scope") != RevisionScope.RESEARCHER_RETURN.value or not upstream_requests:
            return normalized
        normalized["revision_targets"] = [
            target for target in targets if target != "researcher.manager"
        ]
        for request in upstream_requests:
            request.setdefault("target_agent_id", UpstreamRevisionTarget.RESEARCHER_MANAGER.value)
        normalized["upstream_revision_requests"] = upstream_requests
        return normalized

    @model_validator(mode="after")
    def validate_decision(self) -> "DeliberationQualityReviewOutput":
        ready_states = {
            ConclusionReadiness.READY,
            ConclusionReadiness.READY_WITH_CONDITIONS,
        }
        non_ready_states = {
            ConclusionReadiness.NOT_READY,
            ConclusionReadiness.UNDETERMINED,
        }
        if self.status in {
            QualityGateDecision.APPROVED,
            QualityGateDecision.APPROVED_WITH_CONDITIONS,
        }:
            if self.conclusion_readiness not in ready_states:
                raise ValueError("approved review must be Conclusion-ready")
            if self.revision_targets or self.upstream_revision_requests:
                raise ValueError("approved review cannot route revisions")
            if self.revision_scope != RevisionScope.NONE:
                raise ValueError("approved review must use revision_scope=none")
        elif self.status == QualityGateDecision.REVISION_REQUIRED:
            if self.conclusion_readiness not in non_ready_states:
                raise ValueError("revision_required review cannot be Conclusion-ready")
            if not self.findings:
                raise ValueError("revision_required must include findings")
            if not self.revision_targets and not self.upstream_revision_requests:
                raise ValueError("revision_required must route an internal or upstream revision")
            if self.revision_scope == RevisionScope.RESEARCHER_RETURN and not self.upstream_revision_requests:
                raise ValueError("researcher_return requires upstream_revision_requests")
            if self.upstream_revision_requests and self.revision_scope != RevisionScope.RESEARCHER_RETURN:
                raise ValueError("upstream_revision_requests require revision_scope=researcher_return")
        else:
            if self.conclusion_readiness not in non_ready_states:
                raise ValueError("blocked review cannot be Conclusion-ready")
            if not self.blocking_finding_ids:
                raise ValueError("blocked review must identify blocking findings")
            if self.revision_targets or self.upstream_revision_requests:
                raise ValueError(
                    "blocked review cannot contain an executable revision route; "
                    "repairable findings must use revision_required"
                )
            if self.revision_scope != RevisionScope.NONE:
                raise ValueError("blocked review must use revision_scope=none")
        return self
