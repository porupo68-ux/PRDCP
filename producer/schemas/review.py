from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from producer.schemas.research_plan import ResearchPlan


class QualityGateDecision(str, Enum):
    APPROVED = "approved"
    APPROVED_WITH_CONDITIONS = "approved_with_conditions"
    REVISION_REQUIRED = "revision_required"
    BLOCKED = "blocked"


REVISION_TARGETS = {
    "producer.topic_scout",
    "producer.topic_selector",
    "producer.general_opinion_analyst",
    "producer.research_planner",
}


class QualityReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_plan: ResearchPlan
    revision_context: dict | None = None


class QualityReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    status: QualityGateDecision
    revision_target: str | None = None
    reason: str = Field(min_length=1)
    required_action: str | None = None
    approved_research_plan: ResearchPlan | None = None

    @model_validator(mode="after")
    def validate_decision_fields(self) -> "QualityReviewOutput":
        if self.status in {QualityGateDecision.APPROVED, QualityGateDecision.APPROVED_WITH_CONDITIONS}:
            if self.revision_target is not None or self.required_action is not None:
                raise ValueError("approved reviews cannot include revision routing")
            if self.approved_research_plan is None:
                raise ValueError("approved reviews must include approved_research_plan")
        elif self.status == QualityGateDecision.REVISION_REQUIRED:
            if self.revision_target not in REVISION_TARGETS:
                raise ValueError("revision_required must identify a valid specialist agent")
            if not self.required_action:
                raise ValueError("revision_required must include required_action")
        return self

