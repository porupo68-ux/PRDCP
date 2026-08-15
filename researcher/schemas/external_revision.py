from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from producer.schemas.research_plan import ResearchTarget


class ExternalRequiredResearchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_scope: list[str] = Field(default_factory=list)


class ExternalResearchRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_request_id: str = Field(min_length=1)
    target_agent_id: str = Field(min_length=1)
    research_question_id: str = Field(min_length=1)
    affected_claim_ids: list[str] = Field(default_factory=list)
    missing_evidence_description: str = Field(min_length=1)
    preferred_source_categories: list[ResearchTarget] = Field(min_length=1)
    required_scope: ExternalRequiredResearchScope
    acceptance_conditions: list[str] = Field(min_length=1)
    requesting_agent_id: str = Field(min_length=1)
    source_finding_ids: list[str] = Field(min_length=1)

    @field_validator("target_agent_id")
    @classmethod
    def require_researcher_manager(cls, value: str) -> str:
        if value != "researcher.manager":
            raise ValueError("external revision target_agent_id must be researcher.manager")
        return value


class ExternalResearchRevisionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_report_id: str = Field(min_length=1)
    revision_requests: list[ExternalResearchRevisionRequest] = Field(min_length=1)
    quality_review_id: str = Field(min_length=1)


def external_revision_context(
    request: ExternalResearchRevisionRequest,
    *,
    iteration: int,
) -> dict[str, Any]:
    return {
        "revision_source": "deliberation",
        "revision_request_id": request.revision_request_id,
        "source_finding_ids": list(request.source_finding_ids),
        "affected_claim_ids": list(request.affected_claim_ids),
        "missing_evidence_description": request.missing_evidence_description,
        "acceptance_conditions": list(request.acceptance_conditions),
        "required_scope": request.required_scope.model_dump(mode="json"),
        "requesting_agent_id": request.requesting_agent_id,
        "external_revision_iteration": iteration,
    }
