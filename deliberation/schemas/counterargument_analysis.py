from __future__ import annotations

from copy import deepcopy
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deliberation.schemas.identifiers import (
    CHALLENGE_PREFIXES,
    COUNTERARGUMENT_ANALYSIS_PREFIX,
    COUNTERARGUMENT_PREFIXES,
    EVIDENCE_PREFIXES,
    canonicalize_analysis_id,
    require_identifier_list,
)

ChallengeId = Annotated[
    str,
    Field(min_length=1, pattern=r"^(?:challenge_|steelman_).+"),
]
CounterargumentId = Annotated[
    str,
    Field(min_length=1, pattern=r"^(?:counterargument_|counter_).+"),
]
ALTERNATIVE_INTERPRETATION_PREFIX = "alt_interp_"
AlternativeInterpretationId = Annotated[
    str,
    Field(
        min_length=1,
        pattern=r"^alt_interp_.+",
        description=(
            "Counterargument-owned alternative interpretation identifier using "
            "alt_interp_*; distinct from Causal Analyst alternative_/alt_exp_* IDs"
        ),
    ),
]


class Challenge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    challenge_id: ChallengeId
    target_claim_ids: list[str] = Field(min_length=1)
    argument: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    strength: str = Field(min_length=1)

    @field_validator("challenge_id")
    @classmethod
    def validate_challenge_id(cls, value: str) -> str:
        if not value.startswith(CHALLENGE_PREFIXES):
            raise ValueError("challenge_id must use challenge_* or steelman_*")
        return value


RevisionAgentId = Literal[
    "deliberation.argument_analyst",
    "deliberation.causal_structural_analyst",
    "deliberation.stakeholder_response_analyst",
    "deliberation.counterargument_analyst",
    "deliberation.manager",
]
ALLOWED_REVISION_AGENT_IDS = {
    "deliberation.argument_analyst",
    "deliberation.causal_structural_analyst",
    "deliberation.stakeholder_response_analyst",
    "deliberation.counterargument_analyst",
    "deliberation.manager",
}


class CounterargumentBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterargument_id: CounterargumentId
    target_claim_ids: list[str] = Field(min_length=1)
    argument: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    impact: str = Field(min_length=1)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    remaining_uncertainty: str = ""
    research_gap_required: bool

    @field_validator("counterargument_id")
    @classmethod
    def validate_counterargument_id(cls, value: str) -> str:
        if not value.startswith(COUNTERARGUMENT_PREFIXES):
            raise ValueError("counterargument_id must use counterargument_* or counter_*")
        return value

    @field_validator("supporting_evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: list[str]) -> list[str]:
        return require_identifier_list(
            value,
            EVIDENCE_PREFIXES,
            field_name="supporting_evidence_ids",
        )



class RevisionRequiredCounterargument(CounterargumentBase):
    required_revision: Literal[True]
    revision_target_agent_ids: list[RevisionAgentId] = Field(min_length=1)
    acceptance_conditions: list[str] = Field(min_length=1)


class NonRevisionCounterargument(CounterargumentBase):
    required_revision: Literal[False]
    revision_target_agent_ids: list[RevisionAgentId] = Field(max_length=0)
    acceptance_conditions: list[str] = Field(default_factory=list)


Counterargument = RevisionRequiredCounterargument | NonRevisionCounterargument


class IntegrationRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=1)
    target_item_id: str = Field(min_length=1)
    required_change: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_counterargument_ids: list[CounterargumentId] = Field(min_length=1)
    revision_target_agent_ids: list[RevisionAgentId] = Field(min_length=1)
    acceptance_conditions: list[str] = Field(min_length=1)
    research_gap_required: bool

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_revision_shape(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        normalized.setdefault(
            "revision_target_agent_ids",
            ["deliberation.manager"],
        )
        normalized.setdefault(
            "acceptance_conditions",
            ["Record the counterargument disposition in final integration"],
        )
        normalized.setdefault("research_gap_required", False)
        return normalized


class ContraryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ids: list[str] = Field(default_factory=list)
    summary: str = Field(min_length=1)


class ChallengeCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str = Field(min_length=1)


class AlternativeInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation_id: AlternativeInterpretationId
    summary: str = Field(min_length=1)


class OverlookedStakeholder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stakeholder: str = Field(min_length=1)


class FalseBalanceRisk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk: str = Field(min_length=1)


class CounterargumentAnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_id: str = Field(
        min_length=1,
        pattern=r"^counterargument_analysis_.+",
        description="Unique counterargument identifier using counterargument_analysis_*",
    )
    task_id: str = Field(
        min_length=1,
        description="Counterargument task identifier; never an analysis or integration ID",
    )
    steelman_arguments: list[Challenge] = Field(min_length=1)
    counterarguments: list[Counterargument] = Field(min_length=1)
    contrary_evidence: list[ContraryEvidence] = Field(default_factory=list)
    exception_conditions: list[ChallengeCondition] = Field(default_factory=list)
    falsification_conditions: list[ChallengeCondition] = Field(default_factory=list)
    alternative_interpretations: list[AlternativeInterpretation] = Field(default_factory=list)
    overlooked_stakeholders: list[OverlookedStakeholder] = Field(default_factory=list)
    false_balance_risks: list[FalseBalanceRisk] = Field(default_factory=list)
    required_revisions: list[IntegrationRevision] = Field(min_length=1)
    remaining_uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_counterarguments(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized: dict[str, Any] = deepcopy(value)
        revision_ids = {
            counterargument_id
            for revision in normalized.get("required_revisions", [])
            if isinstance(revision, dict)
            for counterargument_id in revision.get("source_counterargument_ids", [])
            if isinstance(counterargument_id, str)
        }
        routed_severities = {
            "blocking",
            "critical",
            "major",
            "high",
            "medium",
            "strong",
            "decisive",
        }
        counterarguments: list[Any] = []
        legacy_counterargument_ids: dict[str, str] = {}
        for raw in normalized.get("counterarguments", []):
            if not isinstance(raw, dict):
                counterarguments.append(raw)
                continue
            item = dict(raw)
            if "counterargument_id" in item:
                item.setdefault("required_revision", False)
                item.setdefault("revision_target_agent_ids", [])
                item.setdefault("remaining_uncertainty", "")
                item.setdefault("research_gap_required", False)
                item.setdefault("acceptance_conditions", [])
                counterarguments.append(item)
                continue
            legacy_challenge_id = str(item.get("challenge_id", ""))
            suffix = re.sub(
                r"[^a-zA-Z0-9_-]+",
                "_",
                legacy_challenge_id,
            ).strip("_")
            counterargument_id = f"counter_legacy_{suffix or 'unknown'}"
            legacy_counterargument_ids[legacy_challenge_id] = counterargument_id
            severity = str(item.get("severity") or item.get("strength") or "undetermined")
            required_revision = (
                counterargument_id in revision_ids
                or severity.lower() in routed_severities
            )
            item.update(
                {
                    "counterargument_id": counterargument_id,
                    "severity": severity,
                    "impact": item.get("impact") or item.get("argument") or "legacy impact not recorded",
                    "supporting_evidence_ids": item.get("evidence_ids", []),
                    "required_revision": required_revision,
                    "revision_target_agent_ids": (
                        ["deliberation.manager"] if required_revision else []
                    ),
                    "remaining_uncertainty": item.get("remaining_uncertainty")
                    or ("Legacy disposition was not explicitly recorded" if required_revision else ""),
                    "research_gap_required": bool(item.get("research_gap_required", False)),
                    "acceptance_conditions": item.get("acceptance_conditions")
                    or (
                        ["Record an explicit revised, rejected, unresolved, or researcher_return disposition"]
                        if required_revision
                        else []
                    ),
                }
            )
            item.pop("challenge_id", None)
            item.pop("strength", None)
            item.pop("evidence_ids", None)
            counterarguments.append(item)
        normalized["counterarguments"] = counterarguments
        for revision in normalized.get("required_revisions", []):
            if not isinstance(revision, dict):
                continue
            revision["source_counterargument_ids"] = [
                legacy_counterargument_ids.get(identifier, identifier)
                for identifier in revision.get("source_counterargument_ids", [])
            ]
        return normalized

    @field_validator("analysis_id", mode="before")
    @classmethod
    def normalize_analysis_id(cls, value: str) -> str:
        return canonicalize_analysis_id(
            value,
            canonical_prefix=COUNTERARGUMENT_ANALYSIS_PREFIX,
            legacy_prefixes=(
                "analysis_counterargument_",
                "integration_initial_",
                "counteranalysis_",
            ),
        )

    @model_validator(mode="after")
    def validate_challenge_ids(self) -> "CounterargumentAnalysisResult":
        ids = [item.challenge_id for item in self.steelman_arguments] + [
            item.counterargument_id for item in self.counterarguments
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("steelman and counterargument IDs must be unique")
        revision_sources = {
            counterargument_id
            for revision in self.required_revisions
            for counterargument_id in revision.source_counterargument_ids
        }
        unknown_sources = revision_sources - {
            item.counterargument_id for item in self.counterarguments
        }
        if unknown_sources:
            raise ValueError(
                f"required_revisions reference unknown counterarguments: {sorted(unknown_sources)}"
            )
        return self

    def unrouted_required_counterargument_ids(self) -> list[str]:
        revision_sources = {
            counterargument_id
            for revision in self.required_revisions
            for counterargument_id in revision.source_counterargument_ids
        }
        return sorted(
            item.counterargument_id
            for item in self.counterarguments
            if item.required_revision and item.counterargument_id not in revision_sources
        )


def normalize_saved_counterargument_payload(
    value: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Repair directly inferable provider-contract errors in a saved raw payload."""

    normalized = deepcopy(value)
    original_analysis_id = str(normalized.get("analysis_id") or "")
    if original_analysis_id.startswith("counteranalysis_"):
        normalized["analysis_id"] = (
            COUNTERARGUMENT_ANALYSIS_PREFIX
            + original_analysis_id[len("counteranalysis_") :]
        )

    removed_targets: dict[str, list[str]] = {}
    for item in normalized.get("counterarguments", []):
        if not isinstance(item, dict):
            continue
        targets = [
            target
            for target in item.get("revision_target_agent_ids", [])
            if isinstance(target, str)
        ]
        kept = [target for target in targets if target in ALLOWED_REVISION_AGENT_IDS]
        if not item.get("required_revision"):
            kept = []
        removed = [target for target in targets if target not in kept]
        if removed:
            removed_targets[str(item.get("counterargument_id") or "unknown")] = removed
        item["revision_target_agent_ids"] = kept

    for item in normalized.get("required_revisions", []):
        if not isinstance(item, dict):
            continue
        targets = [
            target
            for target in item.get("revision_target_agent_ids", [])
            if isinstance(target, str)
        ]
        kept = [target for target in targets if target in ALLOWED_REVISION_AGENT_IDS]
        removed = [target for target in targets if target not in kept]
        if removed:
            removed_targets[str(item.get("revision_id") or "unknown")] = removed
        item["revision_target_agent_ids"] = kept

    return normalized, {
        "analysis_id_before": original_analysis_id,
        "analysis_id_after": normalized.get("analysis_id"),
        "removed_revision_target_agent_ids": removed_targets,
    }
