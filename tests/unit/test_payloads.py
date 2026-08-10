import unittest

from pydantic import ValidationError

from producer.schemas.general_opinion import GeneralOpinion
from producer.schemas.research_plan import ResearchPlan
from producer.schemas.review import QualityReviewOutput
from producer.schemas.topic_scout import TopicScoutOutput


class PayloadSchemaTests(unittest.TestCase):
    def valid_plan(self):
        return {
            "research_plan_id": "plan_1",
            "topic_id": "topic_1",
            "topic": "AIと雇用",
            "general_opinion_id": "opinion_1",
            "general_opinion": "AIは仕事を奪う",
            "research_questions": [
                {
                    "research_question_id": "rq_1",
                    "question": "雇用者数は変化したか",
                    "research_targets": ["ACADEMIC"],
                }
            ],
            "scope": ["日本"],
            "constraints": ["一次情報優先"],
        }

    def test_topic_candidates_cannot_be_empty(self):
        with self.assertRaises(ValidationError):
            TopicScoutOutput.model_validate({"topic_candidates": []})

    def test_research_questions_cannot_exceed_three(self):
        plan = self.valid_plan()
        plan["research_questions"] *= 4
        with self.assertRaises(ValidationError):
            ResearchPlan.model_validate(plan)

    def test_scope_cannot_be_empty(self):
        plan = self.valid_plan()
        plan["scope"] = []
        with self.assertRaises(ValidationError):
            ResearchPlan.model_validate(plan)

    def test_general_opinion_statement_cannot_be_empty(self):
        with self.assertRaises(ValidationError):
            GeneralOpinion.model_validate(
                {
                    "general_opinion_id": "opinion_1",
                    "statement": "",
                    "confidence": 0.5,
                    "evidence_summary": "summary",
                    "supporting_sources": [
                        {"source": "x", "url": "https://example.invalid/x"},
                        {"source": "y", "url": "https://example.invalid/y"},
                        {"source": "z", "url": "https://example.invalid/z"},
                    ],
                }
            )

    def test_general_opinion_requires_three_sources(self):
        with self.assertRaises(ValidationError):
            GeneralOpinion.model_validate(
                {
                    "general_opinion_id": "opinion_1",
                    "statement": "AIは仕事を奪う",
                    "confidence": 0.5,
                    "evidence_summary": "summary",
                    "supporting_sources": [
                        {"source": "x", "url": "https://example.invalid/x"},
                        {"source": "y", "url": "https://example.invalid/y"},
                    ],
                }
            )

    def test_revision_target_must_be_a_specialist(self):
        with self.assertRaises(ValidationError):
            QualityReviewOutput.model_validate(
                {
                    "status": "revision_required",
                    "revision_target": "producer.manager",
                    "reason": "不足",
                    "required_action": "修正",
                    "approved_research_plan": None,
                }
            )

    def test_approved_review_requires_plan(self):
        with self.assertRaises(ValidationError):
            QualityReviewOutput.model_validate(
                {
                    "status": "approved",
                    "revision_target": None,
                    "reason": "ok",
                    "required_action": None,
                    "approved_research_plan": None,
                }
            )


if __name__ == "__main__":
    unittest.main()
