from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationCriterion(str, Enum):
    PROBLEM_RELEVANCE = "PROBLEM_RELEVANCE"
    EXPECTED_EFFECTIVENESS = "EXPECTED_EFFECTIVENESS"
    IMPLEMENTATION_FEASIBILITY = "IMPLEMENTATION_FEASIBILITY"
    COST_AND_RESOURCE_REQUIREMENTS = "COST_AND_RESOURCE_REQUIREMENTS"
    TIME_TO_IMPACT = "TIME_TO_IMPACT"
    STAKEHOLDER_IMPACT = "STAKEHOLDER_IMPACT"
    DISTRIBUTIONAL_EQUITY = "DISTRIBUTIONAL_EQUITY"
    ETHICAL_IMPACT = "ETHICAL_IMPACT"
    LEGAL_FEASIBILITY = "LEGAL_FEASIBILITY"
    POLITICAL_FEASIBILITY = "POLITICAL_FEASIBILITY"
    SCALABILITY = "SCALABILITY"
    REVERSIBILITY = "REVERSIBILITY"
    RISK = "RISK"
    EVIDENCE_SUPPORT = "EVIDENCE_SUPPORT"


class EvaluationRating(str, Enum):
    VERY_LOW = "VERY_LOW"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class ValueProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1)
    priority_criteria: list[EvaluationCriterion] = Field(min_length=1)
    description: str = Field(min_length=1)


class EvaluationFramework(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    evaluation_framework_id: str = Field(min_length=1)
    criteria: list[EvaluationCriterion] = Field(min_length=1)
    rating_scale: list[EvaluationRating] = Field(min_length=6)
    value_profiles: list[ValueProfile] = Field(min_length=1)
    common_time_scope: str = Field(min_length=1)
    common_geographic_scope: str = Field(min_length=1)
    common_target_population: str = Field(min_length=1)
    blocking_issues_are_non_compensatory: bool = True
    not_evaluable_is_not_zero: bool = True

    @model_validator(mode="after")
    def unique_framework_values(self) -> "EvaluationFramework":
        if len(self.criteria) != len(set(self.criteria)):
            raise ValueError("evaluation criteria must be unique")
        profiles = [item.profile_id for item in self.value_profiles]
        if len(profiles) != len(set(profiles)):
            raise ValueError("value profile IDs must be unique")
        required_scale = {item.value for item in EvaluationRating}
        actual_scale = {str(item) for item in self.rating_scale}
        if required_scale != actual_scale:
            raise ValueError("rating_scale must contain the complete canonical ordinal scale")
        return self


DEFAULT_CRITERIA = [item.value for item in EvaluationCriterion]


def default_value_profiles() -> list[dict[str, object]]:
    return [
        {"profile_id": "effectiveness_priority", "priority_criteria": ["EXPECTED_EFFECTIVENESS", "PROBLEM_RELEVANCE"], "description": "問題への直接性と期待効果を優先"},
        {"profile_id": "equity_priority", "priority_criteria": ["DISTRIBUTIONAL_EQUITY", "ETHICAL_IMPACT", "STAKEHOLDER_IMPACT"], "description": "分配、公正、影響主体を優先"},
        {"profile_id": "feasibility_priority", "priority_criteria": ["IMPLEMENTATION_FEASIBILITY", "LEGAL_FEASIBILITY", "POLITICAL_FEASIBILITY"], "description": "実装可能性を優先"},
        {"profile_id": "risk_averse", "priority_criteria": ["RISK", "REVERSIBILITY", "EVIDENCE_SUPPORT"], "description": "重大リスクと不可逆性の回避を優先"},
        {"profile_id": "long_term_priority", "priority_criteria": ["SCALABILITY", "EXPECTED_EFFECTIVENESS", "DISTRIBUTIONAL_EQUITY"], "description": "長期効果と拡張性を優先"},
    ]
