from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnalysisType(str, Enum):
    ARGUMENT = "ARGUMENT"
    CAUSAL_STRUCTURAL = "CAUSAL_STRUCTURAL"
    STAKEHOLDER_RESPONSE = "STAKEHOLDER_RESPONSE"


ANALYSIS_AGENT_MAP = {
    AnalysisType.ARGUMENT: "deliberation.argument_analyst",
    AnalysisType.CAUSAL_STRUCTURAL: "deliberation.causal_structural_analyst",
    AnalysisType.STAKEHOLDER_RESPONSE: "deliberation.stakeholder_response_analyst",
}
PRIMARY_ANALYST_IDS = set(ANALYSIS_AGENT_MAP.values())


class DeliberationAnalysisTask(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    task_id: str = Field(min_length=1)
    analysis_type: AnalysisType
    target_agent_id: str = Field(min_length=1)
    research_report_id: str = Field(min_length=1)
    research_question_ids: list[str] = Field(min_length=1)
    target_evidence_ids: list[str] = Field(min_length=1)
    problem_definition: str = Field(min_length=1)
    shared_definitions: dict[str, str] = Field(default_factory=dict)
    geographic_scope: list[str] = Field(min_length=1)
    time_scope: dict[str, Any] = Field(default_factory=dict)
    analysis_constraints: list[str] = Field(min_length=1)
    completion_conditions: list[str] = Field(min_length=1)
    revision_context: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_assignment(self) -> "DeliberationAnalysisTask":
        expected = ANALYSIS_AGENT_MAP[AnalysisType(self.analysis_type)]
        if self.target_agent_id != expected:
            raise ValueError(f"{self.analysis_type} task must target {expected}")
        if len(set(self.research_question_ids)) != len(self.research_question_ids):
            raise ValueError("research_question_ids must be unique")
        if len(set(self.target_evidence_ids)) != len(self.target_evidence_ids):
            raise ValueError("target_evidence_ids must be unique")
        return self


class CounterargumentTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    target_agent_id: str = "deliberation.counterargument_analyst"
    initial_integration_id: str = Field(min_length=1)
    key_claim_ids: list[str] = Field(min_length=1)
    candidate_viewpoint_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    agreements: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_items: list[dict[str, Any]] = Field(default_factory=list)
    initial_integration: dict[str, Any]
    research_report: dict[str, Any]
    revision_context: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_target(self) -> "CounterargumentTask":
        if self.target_agent_id != "deliberation.counterargument_analyst":
            raise ValueError("Counterargument task has an invalid target_agent_id")
        return self
