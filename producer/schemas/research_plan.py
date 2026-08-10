from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from producer.schemas.general_opinion import GeneralOpinion
from producer.schemas.topic_selector import SelectedTopic


class ResearchTarget(str, Enum):
    EXPERT = "EXPERT"
    ACADEMIC = "ACADEMIC"
    GOVERNMENT = "GOVERNMENT"
    NEWS = "NEWS"
    PUBLIC_OPINION = "PUBLIC_OPINION"
    POLITICIAN = "POLITICIAN"
    INDUSTRY = "INDUSTRY"


class ResearchPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_topic: SelectedTopic
    general_opinion: GeneralOpinion
    revision_context: dict | None = None


class ResearchQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    research_question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    research_targets: list[ResearchTarget] = Field(min_length=1)


class ResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_plan_id: str = Field(min_length=1)
    topic_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    general_opinion_id: str = Field(min_length=1)
    general_opinion: str = Field(min_length=1)
    research_questions: list[ResearchQuestion] = Field(min_length=1, max_length=3)
    scope: list[str] = Field(min_length=1)
    constraints: list[str] = Field(min_length=1)


class ResearchPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    research_plan: ResearchPlan

