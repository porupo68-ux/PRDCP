import unittest

from pydantic import ValidationError

from conclusion.schemas.decision_evaluation import CandidateCriterionEvaluation
from conclusion.schemas.evaluation_framework import EvaluationFramework, EvaluationRating
from conclusion.schemas.position_candidate import PositionCandidate, PositionGenerationResult


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


class ConclusionSchemaTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
