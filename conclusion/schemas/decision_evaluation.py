from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from conclusion.schemas.decision_context import DecisionContext
from conclusion.schemas.evaluation_framework import (
    EvaluationCriterion,
    EvaluationFramework,
    EvaluationRating,
)
from conclusion.schemas.position_candidate import PositionCandidate
from conclusion.schemas.strict_references import (
    bind_strict_reference_fields,
    candidate_reference_values,
    decision_context_reference_values,
    unique_strings,
)


class DecisionEvaluationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    target_agent_id: str = Field(pattern=r"^conclusion\.decision_evaluator$")
    decision_context: DecisionContext
    position_candidates: list[PositionCandidate] = Field(min_length=2, max_length=5)
    evaluation_framework: EvaluationFramework
    revision_context: dict[str, Any] | None = None


class CandidateCriterionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    candidate_id: str = Field(min_length=1)
    criterion: EvaluationCriterion
    rating: EvaluationRating
    rationale: str = Field(min_length=1)
    supporting_claim_ids: list[str] = Field(
        default_factory=list,
        description="Copy exact IDs only from decision_context.key_claim_ids.",
    )
    supporting_evidence_ids: list[str] = Field(
        default_factory=list,
        description="Copy exact IDs only from decision_context.evidence_ids.",
    )
    supporting_analysis_ids: list[str] = Field(
        default_factory=list,
        description="Copy exact IDs only from decision_context.analysis_ids.",
    )
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    blocking_issue: bool = False
    blocking_reason: str | None = None

    @model_validator(mode="after")
    def blocking_reason_required(self) -> "CandidateCriterionEvaluation":
        if self.blocking_issue and not self.blocking_reason:
            raise ValueError("blocking_reason is required for a blocking issue")
        return self


class ComparisonRatings(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    PROBLEM_RELEVANCE: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    EXPECTED_EFFECTIVENESS: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    IMPLEMENTATION_FEASIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    COST_AND_RESOURCE_REQUIREMENTS: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    TIME_TO_IMPACT: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    STAKEHOLDER_IMPACT: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    DISTRIBUTIONAL_EQUITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    ETHICAL_IMPACT: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    LEGAL_FEASIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    POLITICAL_FEASIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    SCALABILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    REVERSIBILITY: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    RISK: EvaluationRating = EvaluationRating.NOT_EVALUABLE
    EVIDENCE_SUPPORT: EvaluationRating = EvaluationRating.NOT_EVALUABLE


class CandidateComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    ratings: ComparisonRatings


class ConditionalAdvantage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1)
    advantaged_candidate_ids: list[str] = Field(min_length=1, max_length=5)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_singular_candidate_id(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "advantaged_candidate_ids" in value:
            return value
        legacy = value.get("advantaged_candidate_id")
        if not isinstance(legacy, str) or not legacy:
            return value
        normalized = dict(value)
        normalized.pop("advantaged_candidate_id", None)
        normalized["advantaged_candidate_ids"] = [legacy]
        return normalized


class DisqualificationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class SensitivityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1)
    preferred_candidate_ids: list[str] = Field(min_length=1, max_length=5)
    reason: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_singular_candidate_id(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "preferred_candidate_ids" in value:
            return value
        legacy = value.get("preferred_candidate_id")
        if not isinstance(legacy, str) or not legacy:
            return value
        normalized = dict(value)
        normalized.pop("preferred_candidate_id", None)
        normalized["preferred_candidate_ids"] = [legacy]
        return normalized


class EvaluationInformationGap(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    criterion: EvaluationCriterion
    status: EvaluationRating


class EvaluationRevisionRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    required_revision: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class DecisionEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision_evaluation_result_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    decision_context_id: str = Field(min_length=1)
    evaluation_framework: EvaluationFramework
    candidate_evaluations: list[CandidateCriterionEvaluation] = Field(
        min_length=2,
        max_length=70,
    )
    comparison_matrix: list[CandidateComparison] = Field(min_length=2, max_length=5)
    conditional_advantages: list[ConditionalAdvantage] = Field(
        default_factory=list,
        max_length=5,
    )
    disqualification_findings: list[DisqualificationFinding] = Field(
        default_factory=list,
        max_length=5,
    )
    sensitivity_analysis: list[SensitivityResult] = Field(min_length=1, max_length=5)
    missing_information: list[EvaluationInformationGap] = Field(
        default_factory=list,
        max_length=70,
    )
    revision_recommendations: list[EvaluationRevisionRecommendation] = Field(
        default_factory=list,
        max_length=20,
    )
    status: str = Field(min_length=1)

    @classmethod
    def specialize_strict_output_schema(
        cls,
        schema: dict[str, Any],
        input_data: dict[str, Any],
    ) -> dict[str, Any]:
        refs = decision_context_reference_values(input_data)
        candidate_ids = candidate_reference_values(input_data)
        context = input_data.get("decision_context")
        context_id = (
            context.get("decision_context_id")
            if isinstance(context, dict)
            else None
        )
        framework = input_data.get("evaluation_framework")
        profiles = framework.get("value_profiles") if isinstance(framework, dict) else []
        profile_ids = unique_strings(
            [
                item.get("profile_id")
                for item in profiles
                if isinstance(item, dict)
            ]
        )
        return bind_strict_reference_fields(
            schema,
            list_fields={
                "advantaged_candidate_ids": candidate_ids,
                "preferred_candidate_ids": candidate_ids,
                "supporting_claim_ids": refs.get("claim", []),
                "supporting_evidence_ids": refs.get("evidence", []),
                "supporting_analysis_ids": refs.get("analysis", []),
            },
            scalar_fields={
                "candidate_id": candidate_ids,
                "profile_id": profile_ids,
                "task_id": unique_strings([input_data.get("task_id")]),
                "decision_context_id": unique_strings([context_id]),
            },
        )

    @model_validator(mode="before")
    @classmethod
    def collapse_exact_duplicate_candidate_evaluations(cls, value: Any) -> Any:
        """Collapse only byte-equivalent semantic repeats from provider generation.

        Compound-key uniqueness cannot be expressed by the strict JSON Schema
        subset. Exact repeats contain no additional information and can be
        removed without changing the evaluation. Conflicting duplicates remain
        present and are rejected by the post-validation integrity checks.
        """

        if not isinstance(value, dict):
            return value
        evaluations = value.get("candidate_evaluations")
        if not isinstance(evaluations, list):
            return value
        seen: dict[tuple[object, object], str] = {}
        normalized_items: list[Any] = []
        changed = False
        for item in evaluations:
            if not isinstance(item, dict):
                normalized_items.append(item)
                continue
            key = (item.get("candidate_id"), item.get("criterion"))
            canonical = json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            prior = seen.get(key)
            if prior is None:
                seen[key] = canonical
                normalized_items.append(item)
            elif prior == canonical:
                changed = True
            else:
                normalized_items.append(item)
        if not changed:
            return value
        normalized = dict(value)
        normalized["candidate_evaluations"] = normalized_items
        return normalized

    @model_validator(mode="after")
    def validate_evaluation_matrix_integrity(self) -> "DecisionEvaluationResult":
        pairs = [(item.candidate_id, item.criterion) for item in self.candidate_evaluations]
        if len(pairs) != len(set(pairs)):
            raise ValueError(
                "conflicting candidate/criterion evaluation pairs must be rejected"
            )

        matrix_ids = [item.candidate_id for item in self.comparison_matrix]
        if len(matrix_ids) != len(set(matrix_ids)):
            raise ValueError("comparison_matrix candidate IDs must be unique")
        candidate_ids = set(matrix_ids)
        evaluated_ids = {item.candidate_id for item in self.candidate_evaluations}
        if evaluated_ids != candidate_ids:
            raise ValueError(
                "candidate_evaluations and comparison_matrix must cover the same candidates"
            )

        expected_criteria = {str(item) for item in self.evaluation_framework.criteria}
        ratings_fields = set(ComparisonRatings.model_fields)
        if expected_criteria != ratings_fields:
            raise ValueError(
                "evaluation framework criteria must match the canonical comparison ratings"
            )
        ratings_by_pair = {
            (item.candidate_id, str(item.criterion)): str(item.rating)
            for item in self.candidate_evaluations
        }
        for row in self.comparison_matrix:
            actual_criteria = {
                criterion
                for candidate_id, criterion in ratings_by_pair
                if candidate_id == row.candidate_id
            }
            if actual_criteria != expected_criteria:
                raise ValueError(
                    f"candidate evaluation criteria are incomplete for {row.candidate_id}"
                )
            for criterion, rating in row.ratings.model_dump(mode="json").items():
                if ratings_by_pair[(row.candidate_id, criterion)] != rating:
                    raise ValueError(
                        "candidate_evaluations and comparison_matrix ratings must agree"
                    )

        profile_ids = {
            profile.profile_id for profile in self.evaluation_framework.value_profiles
        }
        self._validate_profile_candidate_references(
            records=self.conditional_advantages,
            profile_ids=profile_ids,
            candidate_ids=candidate_ids,
            candidate_field="advantaged_candidate_ids",
        )
        self._validate_profile_candidate_references(
            records=self.sensitivity_analysis,
            profile_ids=profile_ids,
            candidate_ids=candidate_ids,
            candidate_field="preferred_candidate_ids",
        )
        for records in (
            self.disqualification_findings,
            self.missing_information,
            self.revision_recommendations,
        ):
            if any(item.candidate_id not in candidate_ids for item in records):
                raise ValueError("evaluation detail references an unknown candidate ID")
        return self

    @staticmethod
    def _validate_profile_candidate_references(
        *,
        records: list[Any],
        profile_ids: set[str],
        candidate_ids: set[str],
        candidate_field: str,
    ) -> None:
        seen_profiles: set[str] = set()
        for record in records:
            if record.profile_id not in profile_ids:
                raise ValueError("evaluation references an unknown value profile ID")
            if record.profile_id in seen_profiles:
                raise ValueError("value profile evaluation records must be unique")
            seen_profiles.add(record.profile_id)
            referenced = list(getattr(record, candidate_field))
            if len(referenced) != len(set(referenced)):
                raise ValueError("profile candidate references must be unique")
            if set(referenced) - candidate_ids:
                raise ValueError("profile evaluation references an unknown candidate ID")
