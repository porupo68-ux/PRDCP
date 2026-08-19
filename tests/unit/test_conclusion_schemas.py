from copy import deepcopy
import unittest

from pydantic import ValidationError

from conclusion.schemas.decision_evaluation import (
    CandidateCriterionEvaluation,
    DecisionEvaluationResult,
)
from conclusion.schemas.evaluation_framework import (
    DEFAULT_CRITERIA,
    EvaluationFramework,
    EvaluationRating,
    default_value_profiles,
)
from conclusion.schemas.position_candidate import PositionCandidate, PositionGenerationResult
from conclusion.schemas.review import ConclusionQualityReviewOutput
from providers.mock.conclusion_fixtures import decision_evaluation


def candidate(candidate_id: str = "position_a") -> dict:
    return {
        "position_candidate_id": candidate_id,
        "title": "候補",
        "summary": "候補の要約",
        "position_type": "policy",
        "normative_direction": "conditional",
        "target_problem_ids": ["problem_1"],
        "target_stakeholder_ids": [],
        "proposed_actions": [{"action": "実施"}],
        "responsible_actors": ["actor"],
        "mechanism_of_action": "仕組み",
        "implementation_steps": ["実施"],
        "time_horizon": "medium",
        "required_resources": [],
        "institutional_requirements": [],
        "expected_benefits": ["便益"],
        "expected_costs": [],
        "risks": [],
        "tradeoffs": [],
        "unintended_consequences": [],
        "supporting_claim_ids": ["claim_1"],
        "supporting_evidence_ids": ["evidence_1"],
        "supporting_analysis_ids": ["analysis_1"],
        "assumptions": [],
        "success_conditions": ["成功条件"],
        "failure_conditions": ["失敗条件"],
        "uncertainties": [],
        "limitations": [],
    }


def evaluation_payload() -> dict:
    framework = {
        "evaluation_framework_id": "framework_1",
        "criteria": DEFAULT_CRITERIA,
        "rating_scale": [item.value for item in EvaluationRating],
        "value_profiles": default_value_profiles(),
        "common_time_scope": "same",
        "common_geographic_scope": "same",
        "common_target_population": "same",
        "blocking_issues_are_non_compensatory": True,
        "not_evaluable_is_not_zero": True,
    }
    return decision_evaluation(
        {
            "task_id": "evaluation_task_1",
            "decision_context": {"decision_context_id": "context_1"},
            "position_candidates": [
                candidate("position_a"),
                candidate("position_b"),
                candidate("position_c"),
            ],
            "evaluation_framework": framework,
        }
    )


class ConclusionSchemaTests(unittest.TestCase):
    @staticmethod
    def _valid_quality_review() -> dict:
        return {
            "review_id": "conclusion_review_1",
            "status": "approved",
            "reason": "valid",
            "playwright_readiness": "ready",
            "findings": [],
            "blocking_finding_ids": [],
            "revision_scope": "none",
            "revision_targets": [],
            "upstream_revision_requests": [],
            "limitations_to_disclose": [],
            "reviewed_candidate_ids": ["position_a"],
            "reviewed_evaluation_result_id": "evaluation_1",
            "reviewed_integration_result_id": "integration_1",
        }

    @staticmethod
    def _finding(finding_id: str = "finding_1") -> dict:
        return {
            "finding_id": finding_id,
            "severity": "MAJOR",
            "category": "traceability",
            "issue": "Conclusion mapping is incomplete",
            "required_action": "Repair the Conclusion mapping",
            "affected_agent_ids": ["conclusion.decision_integrator"],
            "affected_candidate_ids": ["position_a"],
        }

    @staticmethod
    def _upstream_request(finding_id: str = "finding_1") -> dict:
        return {
            "revision_request_id": "upstream_1",
            "affected_candidate_ids": ["position_a"],
            "affected_claim_ids": ["claim_1"],
            "missing_analysis_description": "Missing stakeholder analysis",
            "required_analysis_types": ["stakeholder_response_analysis"],
            "acceptance_conditions": ["Analysis is traceable to evidence"],
            "source_finding_ids": [finding_id],
        }

    def test_legacy_uppercase_playwright_readiness_is_normalized(self):
        payload = self._valid_quality_review()
        payload["playwright_readiness"] = "READY"
        review = ConclusionQualityReviewOutput.model_validate(payload)
        self.assertEqual(review.playwright_readiness, "ready")

    def test_approved_review_cannot_be_not_ready(self):
        payload = self._valid_quality_review()
        payload["playwright_readiness"] = "not_ready"
        with self.assertRaisesRegex(ValidationError, "Playwright-ready"):
            ConclusionQualityReviewOutput.model_validate(payload)

    def test_observed_approved_review_with_revision_routes_is_rejected(self):
        payload = self._valid_quality_review()
        payload.update(
            {
                "status": "approved_with_conditions",
                "playwright_readiness": "ready_with_conditions",
                "findings": [self._finding("qr_trace_gap_claim_task_reorg")],
                "revision_scope": "targeted",
                "revision_targets": [
                    "conclusion.manager",
                    "conclusion.decision_integrator",
                ],
                "upstream_revision_requests": [
                    self._upstream_request("qr_trace_gap_claim_task_reorg")
                ],
                "limitations_to_disclose": ["Traceability mapping is incomplete"],
            }
        )

        with self.assertRaisesRegex(ValidationError, "cannot route revisions"):
            ConclusionQualityReviewOutput.model_validate(payload)

    def test_approved_with_conditions_is_disclosure_only(self):
        payload = self._valid_quality_review()
        payload.update(
            {
                "status": "approved_with_conditions",
                "playwright_readiness": "ready_with_conditions",
                "limitations_to_disclose": ["One feasibility item is NOT_EVALUABLE"],
            }
        )

        review = ConclusionQualityReviewOutput.model_validate(payload)

        self.assertEqual(review.status, "approved_with_conditions")
        payload["limitations_to_disclose"] = []
        with self.assertRaisesRegex(ValidationError, "disclose at least one limitation"):
            ConclusionQualityReviewOutput.model_validate(payload)

    def test_internal_revision_cannot_be_mixed_with_upstream_return(self):
        payload = self._valid_quality_review()
        payload.update(
            {
                "status": "revision_required",
                "playwright_readiness": "not_ready",
                "findings": [self._finding()],
                "revision_scope": "targeted",
                "revision_targets": ["conclusion.decision_integrator"],
                "upstream_revision_requests": [self._upstream_request()],
            }
        )

        with self.assertRaisesRegex(ValidationError, "cannot include upstream"):
            ConclusionQualityReviewOutput.model_validate(payload)

    def test_deliberation_return_cannot_be_mixed_with_internal_revision(self):
        payload = self._valid_quality_review()
        payload.update(
            {
                "status": "revision_required",
                "playwright_readiness": "not_ready",
                "findings": [self._finding()],
                "revision_scope": "deliberation_return",
                "revision_targets": ["conclusion.decision_integrator"],
                "upstream_revision_requests": [self._upstream_request()],
            }
        )

        with self.assertRaisesRegex(ValidationError, "cannot mix internal"):
            ConclusionQualityReviewOutput.model_validate(payload)

    def test_upstream_request_must_reference_a_review_finding(self):
        payload = self._valid_quality_review()
        payload.update(
            {
                "status": "revision_required",
                "playwright_readiness": "not_ready",
                "findings": [self._finding("finding_known")],
                "revision_scope": "deliberation_return",
                "upstream_revision_requests": [
                    self._upstream_request("finding_unknown")
                ],
            }
        )

        with self.assertRaisesRegex(ValidationError, "must reference findings"):
            ConclusionQualityReviewOutput.model_validate(payload)

    def test_internal_revision_target_is_schema_bounded(self):
        payload = self._valid_quality_review()
        payload.update(
            {
                "status": "revision_required",
                "playwright_readiness": "not_ready",
                "findings": [self._finding()],
                "revision_scope": "targeted",
                "revision_targets": ["deliberation.manager"],
            }
        )

        with self.assertRaises(ValidationError):
            ConclusionQualityReviewOutput.model_validate(payload)

    def test_position_requires_mechanism(self):
        data = candidate()
        data["mechanism_of_action"] = ""
        with self.assertRaises(ValidationError):
            PositionCandidate.model_validate(data)

    def test_position_requires_responsible_actor(self):
        data = candidate()
        data["responsible_actors"] = []
        with self.assertRaises(ValidationError):
            PositionCandidate.model_validate(data)

    def test_position_generation_requires_two_to_five_candidates(self):
        with self.assertRaises(ValidationError):
            PositionGenerationResult.model_validate(
                {
                    "position_generation_result_id": "generation_1",
                    "task_id": "task_1",
                    "decision_context_id": "context_1",
                    "position_candidates": [candidate()],
                    "diversity_dimensions": ["actor"],
                }
            )

    def test_position_generation_rejects_duplicate_ids(self):
        with self.assertRaises(ValidationError):
            PositionGenerationResult.model_validate(
                {
                    "position_generation_result_id": "generation_1",
                    "task_id": "task_1",
                    "decision_context_id": "context_1",
                    "position_candidates": [candidate(), candidate()],
                    "diversity_dimensions": ["actor"],
                }
            )

    def test_blocking_evaluation_requires_reason(self):
        with self.assertRaises(ValidationError):
            CandidateCriterionEvaluation.model_validate(
                {
                    "candidate_id": "position_a",
                    "criterion": "LEGAL_FEASIBILITY",
                    "rating": "VERY_LOW",
                    "rationale": "legal conflict",
                    "blocking_issue": True,
                }
            )

    def test_not_evaluable_is_preserved_as_ordinal_value(self):
        item = CandidateCriterionEvaluation.model_validate(
            {
                "candidate_id": "position_a",
                "criterion": "POLITICAL_FEASIBILITY",
                "rating": "NOT_EVALUABLE",
                "rationale": "insufficient information",
                "blocking_issue": False,
            }
        )
        self.assertEqual(item.rating, "NOT_EVALUABLE")

    def test_framework_requires_complete_rating_scale(self):
        with self.assertRaises(ValidationError):
            EvaluationFramework.model_validate(
                {
                    "evaluation_framework_id": "framework_1",
                    "criteria": ["RISK"],
                    "rating_scale": [item.value for item in EvaluationRating if item != EvaluationRating.NOT_EVALUABLE],
                    "value_profiles": [{"profile_id": "risk", "priority_criteria": ["RISK"], "description": "risk"}],
                    "common_time_scope": "same",
                    "common_geographic_scope": "same",
                    "common_target_population": "same",
                }
            )

    def test_exact_candidate_criterion_repetitions_are_losslessly_collapsed(self):
        payload = evaluation_payload()
        expected = deepcopy(payload["candidate_evaluations"])
        payload["candidate_evaluations"] = expected * 4

        result = DecisionEvaluationResult.model_validate(payload)

        self.assertEqual(len(result.candidate_evaluations), len(expected))
        self.assertEqual(
            result.model_dump(mode="json")["candidate_evaluations"],
            expected,
        )

    def test_conflicting_candidate_criterion_repetition_is_rejected(self):
        payload = evaluation_payload()
        conflicting = deepcopy(payload["candidate_evaluations"][0])
        conflicting["rating"] = "VERY_LOW"
        payload["candidate_evaluations"].append(conflicting)

        with self.assertRaisesRegex(ValidationError, "conflicting candidate/criterion"):
            DecisionEvaluationResult.model_validate(payload)

    def test_comparison_matrix_rating_must_match_criterion_evaluation(self):
        payload = evaluation_payload()
        payload["comparison_matrix"][0]["ratings"]["RISK"] = "VERY_LOW"

        with self.assertRaisesRegex(ValidationError, "ratings must agree"):
            DecisionEvaluationResult.model_validate(payload)

    def test_profile_candidate_references_must_be_known_and_separate(self):
        payload = evaluation_payload()
        payload["conditional_advantages"][0]["advantaged_candidate_ids"] = [
            "position_a_or_position_b"
        ]

        with self.assertRaisesRegex(ValidationError, "unknown candidate ID"):
            DecisionEvaluationResult.model_validate(payload)

    def test_legacy_singular_profile_candidate_ids_remain_readable(self):
        payload = evaluation_payload()
        advantage = payload["conditional_advantages"][0]
        advantage["advantaged_candidate_id"] = advantage.pop(
            "advantaged_candidate_ids"
        )[0]
        sensitivity = payload["sensitivity_analysis"][0]
        sensitivity["preferred_candidate_id"] = sensitivity.pop(
            "preferred_candidate_ids"
        )[0]

        result = DecisionEvaluationResult.model_validate(payload)

        self.assertEqual(
            result.conditional_advantages[0].advantaged_candidate_ids,
            ["position_a"],
        )
        self.assertEqual(
            result.sensitivity_analysis[0].preferred_candidate_ids,
            ["position_a"],
        )


if __name__ == "__main__":
    unittest.main()
