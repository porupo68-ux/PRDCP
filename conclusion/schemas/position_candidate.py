from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conclusion.schemas.decision_context import DecisionContext
from conclusion.schemas.strict_references import (
    bind_strict_reference_fields,
    decision_context_reference_values,
    unique_strings,
)


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(min_length=1)
    action: str = Field(min_length=1)


class PositionInformationGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    information_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    affected_candidate_ids: list[str] = Field(default_factory=list)


class PositionCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_candidate_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    position_type: str = Field(min_length=1)
    normative_direction: str = Field(min_length=1)
    target_problem_ids: list[str] = Field(min_length=1)
    target_stakeholder_ids: list[str] = Field(default_factory=list)
    proposed_actions: list[ProposedAction] = Field(min_length=1)
    responsible_actors: list[str] = Field(min_length=1)
    mechanism_of_action: str = Field(min_length=1)
    implementation_steps: list[str] = Field(min_length=1)
    time_horizon: str = Field(min_length=1)
    required_resources: list[str] = Field(default_factory=list)
    institutional_requirements: list[str] = Field(default_factory=list)
    expected_benefits: list[str] = Field(min_length=1)
    expected_costs: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    tradeoffs: list[str] = Field(default_factory=list)
    unintended_consequences: list[str] = Field(default_factory=list)
    supporting_claim_ids: list[str] = Field(
        min_length=1,
        description=(
            "Copy exact IDs only from decision_context.key_claim_ids. Do not use claim "
            "IDs found elsewhere in the full Deliberation payload."
        ),
    )
    supporting_evidence_ids: list[str] = Field(
        min_length=1,
        description="Copy exact IDs only from decision_context.evidence_ids.",
    )
    supporting_analysis_ids: list[str] = Field(
        min_length=1,
        description="Copy exact IDs only from decision_context.analysis_ids.",
    )
    assumptions: list[str] = Field(default_factory=list)
    success_conditions: list[str] = Field(min_length=1)
    failure_conditions: list[str] = Field(min_length=1)
    uncertainties: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PositionGenerationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(pattern=r"^conclusion\.position_generator$")
    decision_context: DecisionContext
    deliberation_result: dict[str, Any]
    requested_candidate_count: int = Field(default=3, ge=2, le=5)
    revision_context: dict[str, Any] | None = None


class PositionGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position_generation_result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    decision_context_id: str = Field(min_length=1)
    position_candidates: list[PositionCandidate] = Field(min_length=2, max_length=5)
    diversity_dimensions: list[str] = Field(min_length=1)
    generation_notes: list[str] = Field(default_factory=list)
    missing_information: list[PositionInformationGap] = Field(default_factory=list)

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        refs = decision_context_reference_values(input_data)
        context = input_data.get("decision_context")
        context_id = (
            context.get("decision_context_id")
            if isinstance(context, dict)
            else None
        )
        return bind_strict_reference_fields(
            schema,
            list_fields={
                "target_problem_ids": refs.get("problem", []),
                "target_stakeholder_ids": refs.get("stakeholder", []),
                "supporting_claim_ids": refs.get("claim", []),
                "supporting_evidence_ids": refs.get("evidence", []),
                "supporting_analysis_ids": refs.get("analysis", []),
            },
            scalar_fields={
                "task_id": unique_strings([input_data.get("task_id")]),
                "decision_context_id": unique_strings([context_id]),
            },
        )

    @model_validator(mode="after")
    def unique_candidate_ids(self) -> "PositionGenerationResult":
        ids = [item.position_candidate_id for item in self.position_candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("position_candidate_id values must be unique")
        return self
