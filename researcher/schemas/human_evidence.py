from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from common.models.pmp import PMPMessage
from researcher.schemas.trace_ids import EvidenceId, SourceId


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ResearchFindingType(str, Enum):
    """Manager-enforced disposition of one Research Quality Review finding."""

    EVIDENCE_SUFFICIENCY = "EVIDENCE_SUFFICIENCY_FINDING"
    HARD_INTEGRITY_FAILURE = "HARD_INTEGRITY_FAILURE"
    UNCLASSIFIED = "UNCLASSIFIED"


class HumanEvidenceDecisionType(str, Enum):
    ACCEPT = "ACCEPT"
    ACCEPT_WITH_LIMITATIONS = "ACCEPT_WITH_LIMITATIONS"
    REVISE = "REVISE"


class HumanActorSource(str, Enum):
    CLI = "cli"
    DISCORD = "discord"
    MOCK_FIXTURE = "mock_fixture"


class _HumanEvidenceIntegrityRepairBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repair_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    quality_review_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    applied_at: datetime = Field(default_factory=utc_now)


class HumanEvidenceSourceReclassificationRepair(_HumanEvidenceIntegrityRepairBase):
    """Repair record for source-classification re-mapping artifacts."""

    repair_kind: Literal["official_industry_source_reclassification"]
    source_id: str = Field(pattern=r"^source_[A-Za-z0-9_.:-]+$")
    previous_source_type: Literal["INDUSTRY"]
    repaired_source_type: Literal["GOVERNMENT"]


class ResearchReportIntegrityRepair(BaseModel):
    """Audited zero-call repair of one persisted Research Report finding.

    This is deliberately separate from the legacy official-host repair model so
    old repair payloads keep their exact serialized shape and Evidence Set hash.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    repair_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    quality_review_id: str = Field(min_length=1)
    finding_id: str = Field(min_length=1)
    repair_kind: Literal[
        "report_limitation_exact_deduplication",
        "recognized_media_source_reclassification",
    ]
    source_id: str | None = Field(
        default=None,
        pattern=r"^source_[A-Za-z0-9_.:-]+$",
    )
    previous_source_type: Literal["EXPERT"] | None = None
    repaired_source_type: Literal["NEWS"] | None = None
    removed_report_limitation_count: int = Field(default=0, ge=0)
    removed_report_limitations: list[str] = Field(default_factory=list)
    report_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_set_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_set_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_calls: Literal[0] = 0
    retrieval_calls: Literal[0] = 0
    rationale: str = Field(min_length=1)
    applied_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_repair_shape(self) -> "ResearchReportIntegrityRepair":
        if self.repair_kind == "report_limitation_exact_deduplication":
            if any(
                value is not None
                for value in (
                    self.source_id,
                    self.previous_source_type,
                    self.repaired_source_type,
                )
            ):
                raise ValueError("limitation repair cannot carry source classification")
            if self.removed_report_limitation_count < 1:
                raise ValueError("limitation repair must remove at least one exact duplicate")
            if not self.removed_report_limitations:
                raise ValueError("limitation repair must identify removed text")
        else:
            if (
                self.source_id is None
                or self.previous_source_type != "EXPERT"
                or self.repaired_source_type != "NEWS"
            ):
                raise ValueError("media repair requires an EXPERT to NEWS source transition")
            if self.removed_report_limitation_count or self.removed_report_limitations:
                raise ValueError("media repair cannot remove report limitations")
        return self


class ResearchSourceDuplicateTrackingRepair(_HumanEvidenceIntegrityRepairBase):
    """Audited relation-only repair for one same-document source family."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    repair_kind: Literal["research_source_duplicate_tracking"]
    repair_type: Literal["RESEARCH_SOURCE_DUPLICATE_TRACKING_REPAIR"] = (
        "RESEARCH_SOURCE_DUPLICATE_TRACKING_REPAIR"
    )
    document_family_id: str = Field(pattern=r"^docfam_[a-f0-9]{24}$")
    canonical_source_id: SourceId
    canonical_evidence_id: EvidenceId
    related_source_ids: list[SourceId] = Field(min_length=1)
    merged_evidence_ids: list[EvidenceId] = Field(min_length=1)
    report_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_metadata_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    relation_metadata_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_content_sha256_before: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_content_sha256_after: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_calls: Literal[0] = 0
    retrieval_calls: Literal[0] = 0

    @model_validator(mode="after")
    def validate_duplicate_tracking_shape(self) -> "ResearchSourceDuplicateTrackingRepair":
        if len(self.related_source_ids) != len(set(self.related_source_ids)):
            raise ValueError("related_source_ids must be unique")
        if len(self.merged_evidence_ids) != len(set(self.merged_evidence_ids)):
            raise ValueError("merged_evidence_ids must be unique")
        if self.canonical_source_id in self.related_source_ids:
            raise ValueError("canonical_source_id cannot also be related")
        if self.canonical_evidence_id in self.merged_evidence_ids:
            raise ValueError("canonical_evidence_id cannot merge itself")
        if len(self.related_source_ids) != len(self.merged_evidence_ids):
            raise ValueError("each related source must contribute one merged evidence ID")
        if (
            self.relation_metadata_sha256_before
            == self.relation_metadata_sha256_after
            and self.report_sha256_before != self.report_sha256_after
        ):
            raise ValueError("no-op relation repair cannot change the Report")
        if self.immutable_content_sha256_before != self.immutable_content_sha256_after:
            raise ValueError("duplicate tracking repair cannot change protected content")
        return self


HumanEvidenceIntegrityRepair: TypeAlias = Annotated[
    HumanEvidenceSourceReclassificationRepair
    | ResearchReportIntegrityRepair
    | ResearchSourceDuplicateTrackingRepair,
    Field(discriminator="repair_kind"),
]

HumanEvidenceIntegrityRepairRecord: TypeAlias = HumanEvidenceIntegrityRepair

_HUMAN_EVIDENCE_INTEGRITY_REPAIR_ADAPTER = TypeAdapter(
    HumanEvidenceIntegrityRepair
)


def validate_human_evidence_integrity_repair(
    value: object,
) -> HumanEvidenceIntegrityRepair:
    """Validate persisted repairs through the canonical discriminated union."""

    if not isinstance(value, dict):
        raise ValueError("Human Evidence Integrity Repair payload must be an object")
    repair_kind = value.get("repair_kind")
    supported_kinds = {
        "official_industry_source_reclassification",
        "report_limitation_exact_deduplication",
        "recognized_media_source_reclassification",
        "research_source_duplicate_tracking",
    }
    if repair_kind not in supported_kinds:
        raise ValueError(
            f"Unsupported HumanEvidenceIntegrityRepair kind: {repair_kind}"
        )
    try:
        return _HUMAN_EVIDENCE_INTEGRITY_REPAIR_ADAPTER.validate_python(value)
    except Exception as exc:
        raise ValueError(
            f"Invalid HumanEvidenceIntegrityRepair artifact: {exc}"
        ) from exc


class EvidenceFindingDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    finding_id: str = Field(min_length=1)
    finding_type: ResearchFindingType
    severity: str = Field(min_length=1)
    research_question_id: str | None = None
    target_agent_id: str | None = None
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    resolved: bool = False
    resolution: str | None = None


class EvidenceRevisionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    quality_review_id: str = Field(min_length=1)
    human_decision_id: str = Field(min_length=1)
    finding_ids: list[str] = Field(min_length=1)
    target_agent_ids: list[str] = Field(min_length=1)
    estimated_max_retrieval_calls: int = Field(ge=0)
    estimated_max_reasoning_calls: int = Field(ge=0)
    estimated_quality_review_calls: int = Field(default=1, ge=1, le=1)
    estimated_max_provider_calls: int = Field(ge=1)
    provider_authorization_required: bool = True
    created_at: datetime = Field(default_factory=utc_now)


class HumanEvidenceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    schema_name: str = Field(default="PRDCP Human Evidence Decision")
    schema_version: str = Field(default="1.0.0", pattern=r"^1\.0\.0$")
    decision_id: str = Field(min_length=1)
    workflow_id: str = Field(min_length=1)
    quality_review_id: str = Field(min_length=1)
    evidence_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    human_gate_revision: int = Field(ge=1)
    decision: HumanEvidenceDecisionType
    accepted_finding_ids: list[str] = Field(default_factory=list)
    revision_requested_finding_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    actor_type: str = Field(default="human_operator", pattern=r"^human_operator$")
    actor_source: HumanActorSource
    provider_calls_authorized: bool = False
    decided_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_routes(self) -> "HumanEvidenceDecision":
        accepted = set(self.accepted_finding_ids)
        revision = set(self.revision_requested_finding_ids)
        if len(accepted) != len(self.accepted_finding_ids):
            raise ValueError("accepted_finding_ids must be unique")
        if len(revision) != len(self.revision_requested_finding_ids):
            raise ValueError("revision_requested_finding_ids must be unique")
        if accepted & revision:
            raise ValueError("a finding cannot be both accepted and routed to revision")
        if self.decision == HumanEvidenceDecisionType.ACCEPT:
            if accepted or revision:
                raise ValueError("ACCEPT cannot carry accepted or revision finding IDs")
        elif self.decision == HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS:
            if not accepted or revision:
                raise ValueError(
                    "ACCEPT_WITH_LIMITATIONS requires accepted findings and no revision route"
                )
        elif not revision or accepted:
            raise ValueError("REVISE requires revision findings and no accepted findings")
        if self.provider_calls_authorized:
            raise ValueError("Human Evidence Decision cannot authorize Provider calls")
        return self


class AcceptedEvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str = Field(min_length=1)
    quality_review_id: str = Field(min_length=1)
    human_decision_id: str = Field(min_length=1)
    research_question_id: str | None = None
    issue: str = Field(min_length=1)
    required_action: str = Field(min_length=1)
    status: str = Field(default="accepted_unresolved", pattern=r"^accepted_unresolved$")
    factual_support_confirmed: bool = False

    @model_validator(mode="after")
    def preserve_uncertainty(self) -> "AcceptedEvidenceGap":
        if self.factual_support_confirmed:
            raise ValueError("Human acceptance cannot confirm factual support")
        return self


class HumanEvidenceDecisionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: HumanEvidenceDecision
    decision_message: PMPMessage


class HumanEvidenceGateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    workflow_id: str = Field(min_length=1)
    research_report_id: str = Field(min_length=1)
    quality_review_id: str = Field(min_length=1)
    evidence_set_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_count: int = Field(ge=0)
    quality_review_status: str = Field(min_length=1)
    recommended_action: str = Field(min_length=1)
    hard_integrity_findings: list[EvidenceFindingDisposition]
    evidence_sufficiency_findings: list[EvidenceFindingDisposition]
    resolved_integrity_findings: list[EvidenceFindingDisposition]
    unclassified_findings: list[EvidenceFindingDisposition]
    eligible: bool
    existing_decision: HumanEvidenceDecision | None = None
    revision_plan: EvidenceRevisionPlan | None = None
