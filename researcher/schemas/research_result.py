from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from researcher.schemas.research_task import RESEARCHER_AGENT_IDS
from researcher.schemas.source import ResearchSource


class CoverageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    NO_RESULT = "NO_RESULT"


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    task_id: str = Field(min_length=1)
    research_question_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    sources: list[ResearchSource] = Field(default_factory=list)
    search_summary: str = Field(min_length=1)
    coverage_status: CoverageStatus
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_coverage(self) -> "ResearchResult":
        if self.agent_id not in RESEARCHER_AGENT_IDS:
            raise ValueError("agent_id must identify a specialist Researcher")
        if self.coverage_status == CoverageStatus.COMPLETE and not self.sources:
            raise ValueError("COMPLETE result must include at least one source")
        if self.coverage_status == CoverageStatus.NO_RESULT and self.sources:
            raise ValueError("NO_RESULT cannot include sources")
        for source in self.sources:
            if self.research_question_id not in source.research_question_ids:
                raise ValueError("every source must trace to the result research_question_id")
        return self
