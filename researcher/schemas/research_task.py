from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from producer.schemas.research_plan import ResearchTarget


RESEARCH_TARGET_MAP = {
    ResearchTarget.EXPERT: "researcher.expert_researcher",
    ResearchTarget.ACADEMIC: "researcher.academic_researcher",
    ResearchTarget.GOVERNMENT: "researcher.government_researcher",
    ResearchTarget.NEWS: "researcher.news_researcher",
    ResearchTarget.PUBLIC_OPINION: "researcher.public_opinion_researcher",
    ResearchTarget.POLITICIAN: "researcher.politician_researcher",
    ResearchTarget.INDUSTRY: "researcher.industry_researcher",
}

RESEARCHER_AGENT_IDS = {value for value in RESEARCH_TARGET_MAP.values()}


class ResearchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    task_id: str = Field(min_length=1)
    research_question_id: str = Field(min_length=1)
    target_agent_id: str = Field(min_length=1)
    research_target: ResearchTarget
    question: str = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    constraints: list[str] = Field(min_length=1)
    max_sources: int = Field(default=5, ge=1, le=20)
    revision_context: dict[str, Any] | None = None

    @model_validator(mode="after")
    def target_matches_agent(self) -> "ResearchTask":
        expected = RESEARCH_TARGET_MAP[ResearchTarget(self.research_target)]
        if self.target_agent_id != expected:
            raise ValueError(
                f"research_target {self.research_target} must route to {expected}"
            )
        return self
