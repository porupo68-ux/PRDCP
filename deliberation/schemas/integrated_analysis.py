from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deliberation.schemas.causal_structural_analysis import CausalCondition, CausationRisk
from deliberation.schemas.identifiers import (
    ANALYSIS_PREFIXES,
    CLAIM_PREFIXES,
    COUNTERARGUMENT_PREFIXES,
    EVIDENCE_PREFIXES,
    FINAL_INTEGRATION_PREFIX,
    INITIAL_INTEGRATION_PREFIX,
    INTEGRATION_PREFIXES,
    SOURCE_PREFIXES,
    TASK_PREFIXES,
    canonicalize_analysis_reference,
    require_identifier_list,
    require_identifier_prefix,
)


def _canonicalize_analysis_source(value: str) -> str:
    if value == "deliberation.manager":
        return value
    return canonicalize_analysis_reference(value)


def _canonicalize_analysis_sources(values: list[str]) -> list[str]:
    normalized = [_canonicalize_analysis_source(value) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("analysis references must not contain duplicate IDs")
    return normalized


class Viewpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    viewpoint_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    position: str = Field(min_length=1)
    supporting_claim_ids: list[str] = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(min_length=1)
    counterarguments: list[str] = Field(default_factory=list)
    strongest_objections: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    scope_conditions: list[str] = Field(default_factory=list)


class ProblemScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    geographic: list[str] = Field(default_factory=list)
    temporal: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)


class ProblemDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1)
    general_opinion_under_review: str = Field(min_length=1)
    refined_problem_statement: str = Field(min_length=1)
    scope: ProblemScope = Field(default_factory=ProblemScope)
    key_dimensions: list[str] = Field(default_factory=list)
    source_analysis_ids: list[str] = Field(default_factory=list)
    revision_note: str | None = None

    @field_validator("source_analysis_ids", mode="before")
    @classmethod
    def normalize_source_analysis_ids(cls, value: list[str]) -> list[str]:
        return _canonicalize_analysis_sources(value)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "general_opinion" not in value:
            return value
        return {
            "topic": value.get("topic"),
            "general_opinion_under_review": value.get("general_opinion"),
            "refined_problem_statement": value.get("definition"),
            "scope": {"geographic": [], "temporal": [], "domain": []},
            "key_dimensions": [],
            "source_analysis_ids": [],
            "revision_note": None,
        }


class IntegratedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    claim_type: str = Field(default="UNSPECIFIED", min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    note: str = ""
    source_analysis_id: str = Field(default="deliberation.manager", min_length=1)
    support_status: str = Field(default="UNSPECIFIED", min_length=1)
    revision_reflected: str | None = None

    @field_validator("source_analysis_id", mode="before")
    @classmethod
    def normalize_source_analysis_id(cls, value: str) -> str:
        return _canonicalize_analysis_source(value)

    @model_validator(mode="before")
    @classmethod
    def remove_legacy_importance(cls, value: object) -> object:
        if not isinstance(value, dict) or "importance" not in value:
            return value
        normalized = dict(value)
        normalized.pop("importance", None)
        return normalized


class IntegratedCausalItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: str = Field(min_length=1)
    status_note: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class IntegratedConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    necessary: list[CausalCondition] = Field(default_factory=list)
    sufficient: list[CausalCondition] = Field(default_factory=list)


class IntegratedAlternativeExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_linked: list[str] = Field(default_factory=list)
    source_counterargument_ids: list[str] = Field(default_factory=list)


class EvidenceStrengthAsymmetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    source_counterargument_ids: list[str] = Field(default_factory=list)


class IntegratedCausalStructure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_analysis_id: str = Field(default="deliberation.manager", min_length=1)
    causal_claims: list[IntegratedCausalItem] = Field(default_factory=list)
    mechanisms: list[IntegratedCausalItem] = Field(default_factory=list)
    structural_factors: list[IntegratedCausalItem] = Field(default_factory=list)
    feedback_loops: list[IntegratedCausalItem] = Field(default_factory=list)
    alternative_explanations: list[IntegratedAlternativeExplanation] = Field(
        default_factory=list
    )
    conditions: IntegratedConditions = Field(default_factory=IntegratedConditions)
    correlation_causation_risks: list[CausationRisk] = Field(default_factory=list)
    evidence_strength_asymmetry: EvidenceStrengthAsymmetry | None = None
    net_effect_assessment: str = ""
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("source_analysis_id", mode="before")
    @classmethod
    def normalize_source_analysis_id(cls, value: str) -> str:
        return _canonicalize_analysis_source(value)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "summary" not in value:
            return value
        factors = [
            {
                "item_id": f"mock_structural_factor_{index}",
                "description": str(description),
                "status": "UNSPECIFIED",
                "status_note": None,
                "evidence_ids": [],
            }
            for index, description in enumerate(
                value.get("structural_factors", []), start=1
            )
        ]
        return {
            "source_analysis_id": "deliberation.manager",
            "causal_claims": [],
            "mechanisms": [],
            "structural_factors": factors,
            "feedback_loops": [],
            "alternative_explanations": [],
            "conditions": {"necessary": [], "sufficient": []},
            "correlation_causation_risks": [],
            "evidence_strength_asymmetry": None,
            "net_effect_assessment": value.get("summary", ""),
            "uncertainties": [],
        }


class IntegratedStakeholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stakeholder_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class IntegratedImplementationBarrier(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    type: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)


class IntegratedDistributionalEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    affected_group: str = Field(min_length=1)
    effect_type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    uncertainty: str = ""


class IntegratedStakeholderStructure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_analysis_id: str = Field(default="deliberation.manager", min_length=1)
    verification_caveat: str = ""
    stakeholders: list[IntegratedStakeholder] = Field(default_factory=list)
    interest_conflicts: list[str] = Field(default_factory=list)
    authority_and_capacity_summary: list[str] = Field(default_factory=list)
    implementation_barriers: list[IntegratedImplementationBarrier] = Field(
        default_factory=list
    )
    distributional_effects: list[IntegratedDistributionalEffect] = Field(
        default_factory=list
    )
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("source_analysis_id", mode="before")
    @classmethod
    def normalize_source_analysis_id(cls, value: str) -> str:
        return _canonicalize_analysis_source(value)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "primary" not in value:
            return value
        return {
            "source_analysis_id": "deliberation.manager",
            "verification_caveat": str(value.get("distribution", "")),
            "stakeholders": [
                {
                    "stakeholder_id": f"mock_stakeholder_{index}",
                    "name": str(name),
                    "role": "UNSPECIFIED",
                    "evidence_ids": [],
                }
                for index, name in enumerate(value.get("primary", []), start=1)
            ],
            "interest_conflicts": [],
            "authority_and_capacity_summary": [],
            "implementation_barriers": [],
            "distributional_effects": [],
            "uncertainties": [],
        }


class ExistingResponseAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_id: str = Field(default="unspecified_response", min_length=1)
    actor: str = ""
    description: str = Field(min_length=1)
    implementation_status: str = Field(default="UNSPECIFIED", min_length=1)
    effectiveness: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    verification_caveat: str = ""
    barriers: list[str] = Field(default_factory=list)
    side_effects: str = ""

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_mock_shape(cls, value: object) -> object:
        if not isinstance(value, dict) or "response" not in value:
            return value
        return {
            "response_id": "mock_response",
            "actor": "",
            "description": value.get("response"),
            "implementation_status": "UNSPECIFIED",
            "effectiveness": value.get("assessment"),
            "evidence_ids": [],
            "verification_caveat": "",
            "barriers": [],
            "side_effects": "",
        }


class AgreementSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agreement_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    supporting_analysis_ids: list[str] = Field(default_factory=list)

    @field_validator("supporting_analysis_ids", mode="before")
    @classmethod
    def normalize_supporting_analysis_ids(cls, value: list[str]) -> list[str]:
        return _canonicalize_analysis_sources(value)

    @model_validator(mode="before")
    @classmethod
    def accept_summary_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "summary" not in value:
            return value
        normalized = dict(value)
        normalized["description"] = normalized.pop("summary")
        return normalized


class ConflictSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    involved_analysis_ids: list[str] = Field(default_factory=list)
    status: str = Field(default="UNRESOLVED", min_length=1)
    integration_note: str = ""

    @field_validator("involved_analysis_ids", mode="before")
    @classmethod
    def normalize_involved_analysis_ids(cls, value: list[str]) -> list[str]:
        return _canonicalize_analysis_sources(value)

    @model_validator(mode="before")
    @classmethod
    def accept_summary_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "summary" not in value:
            return value
        normalized = dict(value)
        normalized["description"] = normalized.pop("summary")
        return normalized


class UnresolvedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    affected_claim_ids: list[str] = Field(default_factory=list)
    required_input: str = ""
    routing_option: str = ""

    @model_validator(mode="before")
    @classmethod
    def accept_summary_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "summary" not in value:
            return value
        normalized = dict(value)
        normalized["description"] = normalized.pop("summary")
        return normalized


class TradeoffSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tradeoff_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    related_claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def accept_summary_alias(cls, value: object) -> object:
        if not isinstance(value, dict) or "summary" not in value:
            return value
        normalized = dict(value)
        normalized["description"] = normalized.pop("summary")
        return normalized


class TraceabilityEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "2.0"
    claim_ids: list[str] = Field(default_factory=list)
    viewpoint_ids: list[str] = Field(default_factory=list)
    causal_item_ids: list[str] = Field(default_factory=list)
    integration_change_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    analysis_ids: list[str] = Field(default_factory=list)
    counterargument_ids: list[str] = Field(default_factory=list)
    integration_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def split_legacy_evidence_bucket(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        is_legacy = (
            "claim_id" in normalized
            or normalized.get("schema_version") == "1.0"
        )
        if not is_legacy:
            return normalized
        normalized["schema_version"] = "1.0"
        if "claim_id" in normalized:
            normalized.setdefault("claim_ids", []).append(normalized.pop("claim_id"))
        buckets = {
            "claim_ids": list(normalized.get("claim_ids", [])),
            "viewpoint_ids": list(normalized.get("viewpoint_ids", [])),
            "causal_item_ids": list(normalized.get("causal_item_ids", [])),
            "integration_change_ids": list(
                normalized.get("integration_change_ids", [])
            ),
            "evidence_ids": list(normalized.get("evidence_ids", [])),
            "source_ids": list(normalized.get("source_ids", [])),
            "analysis_ids": list(normalized.get("analysis_ids", [])),
            "counterargument_ids": list(normalized.get("counterargument_ids", [])),
            "integration_ids": list(normalized.get("integration_ids", [])),
            "task_ids": list(normalized.get("task_ids", [])),
        }
        buckets["evidence_ids"] = []
        for identifier in normalized.get("evidence_ids", []):
            if not isinstance(identifier, str):
                buckets["evidence_ids"].append(identifier)
            elif identifier.startswith(EVIDENCE_PREFIXES):
                buckets["evidence_ids"].append(identifier)
            elif identifier.startswith(SOURCE_PREFIXES):
                buckets["source_ids"].append(identifier)
            elif identifier.startswith(TASK_PREFIXES):
                buckets["task_ids"].append(identifier)
            elif identifier.startswith(INTEGRATION_PREFIXES):
                buckets["integration_ids"].append(identifier)
            elif identifier.startswith(CLAIM_PREFIXES):
                buckets["claim_ids"].append(identifier)
            elif identifier.startswith(("viewpoint_", "vp_")):
                buckets["viewpoint_ids"].append(identifier)
            elif identifier.startswith("change_"):
                buckets["integration_change_ids"].append(identifier)
            else:
                try:
                    buckets["analysis_ids"].append(
                        canonicalize_analysis_reference(identifier)
                    )
                except ValueError:
                    if identifier.startswith(COUNTERARGUMENT_PREFIXES):
                        buckets["counterargument_ids"].append(identifier)
                    elif identifier.startswith(
                        (
                            "causal_",
                            "mechanism_",
                            "structure_",
                            "structural_",
                            "feedback_",
                            "alternative_",
                            "alt_exp_",
                        )
                    ):
                        buckets["causal_item_ids"].append(identifier)
                    else:
                        buckets["evidence_ids"].append(identifier)
        for field_name, identifiers in buckets.items():
            normalized[field_name] = list(dict.fromkeys(identifiers))
        return normalized

    @field_validator("claim_ids")
    @classmethod
    def validate_claim_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(value, CLAIM_PREFIXES, field_name="claim_ids")

    @field_validator("viewpoint_ids")
    @classmethod
    def validate_viewpoint_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            ("viewpoint_", "vp_"),
            field_name="viewpoint_ids",
        )

    @field_validator("causal_item_ids")
    @classmethod
    def validate_causal_item_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            (
                "causal_",
                "mechanism_",
                "structure_",
                "structural_",
                "feedback_",
                "alternative_",
                "alt_exp_",
            ),
            field_name="causal_item_ids",
        )

    @field_validator("integration_change_ids")
    @classmethod
    def validate_change_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            ("change_",),
            field_name="integration_change_ids",
        )

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            EVIDENCE_PREFIXES,
            field_name="evidence_ids",
        )

    @field_validator("source_ids")
    @classmethod
    def validate_source_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(value, SOURCE_PREFIXES, field_name="source_ids")

    @field_validator("analysis_ids", mode="before")
    @classmethod
    def normalize_analysis_ids(cls, value: list[str]) -> list[str]:
        normalized = [canonicalize_analysis_reference(item) for item in value]
        return require_identifier_list(
            normalized,
            ANALYSIS_PREFIXES,
            field_name="analysis_ids",
        )

    @field_validator("counterargument_ids")
    @classmethod
    def validate_counterargument_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            COUNTERARGUMENT_PREFIXES,
            field_name="counterargument_ids",
        )

    @field_validator("integration_ids")
    @classmethod
    def validate_integration_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            INTEGRATION_PREFIXES,
            field_name="integration_ids",
        )

    @field_validator("task_ids")
    @classmethod
    def validate_task_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(value, TASK_PREFIXES, field_name="task_ids")


def _normalize_traceability(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: list[dict[str, Any]] = []
        for subject_id, legacy_ids in value.items():
            entry: dict[str, Any] = {
                "schema_version": "1.0",
                "claim_ids": [],
                "viewpoint_ids": [],
                "causal_item_ids": [],
                "integration_change_ids": [],
                "evidence_ids": legacy_ids,
                "source_ids": [],
                "analysis_ids": [],
                "counterargument_ids": [],
                "integration_ids": [],
                "task_ids": [],
            }
            if str(subject_id).startswith(CLAIM_PREFIXES):
                entry["claim_ids"] = [subject_id]
            elif str(subject_id).startswith(("viewpoint_", "vp_")):
                entry["viewpoint_ids"] = [subject_id]
            elif str(subject_id).startswith("change_"):
                entry["integration_change_ids"] = [subject_id]
            else:
                entry["causal_item_ids"] = [subject_id]
            normalized.append(entry)
        return normalized
    return value


class InitialIntegratedAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_id: str = Field(min_length=1)
    problem_definition: ProblemDefinition
    key_claims: list[IntegratedClaim] = Field(min_length=1)
    causal_structure: IntegratedCausalStructure
    stakeholder_structure: IntegratedStakeholderStructure
    existing_response_assessment: list[ExistingResponseAssessment] = Field(default_factory=list)
    agreements: list[AgreementSummary] = Field(default_factory=list)
    conflicts: list[ConflictSummary] = Field(default_factory=list)
    unresolved_items: list[UnresolvedItem] = Field(default_factory=list)
    candidate_viewpoints: list[Viewpoint] = Field(min_length=1, max_length=3)
    traceability_index: list[TraceabilityEntry] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("integration_id")
    @classmethod
    def validate_integration_id(cls, value: str) -> str:
        return require_identifier_prefix(
            value,
            (INITIAL_INTEGRATION_PREFIX,),
            field_name="integration_id",
        )

    @field_validator("traceability_index", mode="before")
    @classmethod
    def accept_legacy_traceability_map(cls, value: Any) -> Any:
        return _normalize_traceability(value)

    @model_validator(mode="after")
    def validate_claims(self) -> "InitialIntegratedAnalysis":
        claim_ids: list[str] = []
        for claim in self.key_claims:
            claim_ids.append(claim.claim_id)
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("integrated key claims must have unique claim_id values")
        return self


class IntegrationChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_id: str = Field(min_length=1)
    target_item_id: str = Field(min_length=1)
    change_type: str = Field(min_length=1)
    before_summary: str = Field(min_length=1)
    after_summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_counterargument_ids: list[str] = Field(min_length=1)


class CounterargumentDisposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterargument_id: str = Field(min_length=1)
    resolution: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    revision_target_agent_ids: list[str] = Field(default_factory=list)
    integration_change_ids: list[str] = Field(default_factory=list)
    remaining_uncertainty: str = ""
    research_gap_required: bool
    acceptance_conditions: list[str] = Field(default_factory=list)

    @field_validator("counterargument_id")
    @classmethod
    def validate_counterargument_id(cls, value: str) -> str:
        return require_identifier_prefix(
            value,
            COUNTERARGUMENT_PREFIXES,
            field_name="counterargument_id",
        )

    @field_validator("integration_change_ids")
    @classmethod
    def validate_integration_change_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            ("change_",),
            field_name="integration_change_ids",
        )

    @field_validator("resolution")
    @classmethod
    def validate_resolution(cls, value: str) -> str:
        allowed = {"revised", "rejected", "unresolved", "researcher_return"}
        if value not in allowed:
            raise ValueError(f"resolution must be one of {sorted(allowed)}")
        return value

    @model_validator(mode="after")
    def validate_resolution_details(self) -> "CounterargumentDisposition":
        if self.resolution == "revised" and not self.integration_change_ids:
            raise ValueError("revised counterarguments require integration_change_ids")
        if self.resolution == "researcher_return" and not self.research_gap_required:
            raise ValueError("researcher_return requires research_gap_required=true")
        if self.resolution == "unresolved" and not self.remaining_uncertainty:
            raise ValueError("unresolved counterarguments require remaining_uncertainty")
        return self


class FinalIntegratedAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    integration_id: str = Field(min_length=1)
    previous_integration_id: str = Field(min_length=1)
    problem_definition: ProblemDefinition
    key_claims: list[IntegratedClaim] = Field(min_length=1)
    causal_structure: IntegratedCausalStructure
    stakeholder_structure: IntegratedStakeholderStructure
    existing_response_assessment: list[ExistingResponseAssessment] = Field(default_factory=list)
    major_viewpoints: list[Viewpoint] = Field(min_length=1, max_length=3)
    agreements: list[AgreementSummary] = Field(default_factory=list)
    disagreements: list[ConflictSummary] = Field(default_factory=list)
    tradeoffs: list[TradeoffSummary] = Field(default_factory=list)
    unresolved_questions: list[UnresolvedItem] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    integration_changes: list[IntegrationChange] = Field(min_length=1)
    counterargument_dispositions: list[CounterargumentDisposition] = Field(
        default_factory=list
    )
    traceability_index: list[TraceabilityEntry] = Field(default_factory=list)

    @field_validator("traceability_index", mode="before")
    @classmethod
    def accept_legacy_traceability_map(cls, value: Any) -> Any:
        return _normalize_traceability(value)

    @model_validator(mode="after")
    def validate_lineage(self) -> "FinalIntegratedAnalysis":
        if self.integration_id == self.previous_integration_id:
            raise ValueError("final integration must have a new integration_id")
        ids = [item.viewpoint_id for item in self.major_viewpoints]
        if len(set(ids)) != len(ids):
            raise ValueError("major_viewpoints must have unique viewpoint_id values")
        claim_ids = [claim.claim_id for claim in self.key_claims]
        if len(set(claim_ids)) != len(claim_ids):
            raise ValueError("final key claims must have unique claim_id values")
        disposition_ids = [
            item.counterargument_id for item in self.counterargument_dispositions
        ]
        if len(disposition_ids) != len(set(disposition_ids)):
            raise ValueError(
                "counterargument_dispositions must have unique counterargument_id values"
            )
        return self

    @field_validator("integration_id")
    @classmethod
    def validate_integration_id(cls, value: str) -> str:
        return require_identifier_prefix(
            value,
            (FINAL_INTEGRATION_PREFIX,),
            field_name="integration_id",
        )

    @field_validator("previous_integration_id")
    @classmethod
    def validate_previous_integration_id(cls, value: str) -> str:
        return require_identifier_prefix(
            value,
            (INITIAL_INTEGRATION_PREFIX,),
            field_name="previous_integration_id",
        )
