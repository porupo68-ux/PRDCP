import unittest

from pydantic import ValidationError

from researcher.schemas.research_result import ResearchResult
from researcher.schemas.research_task import ResearchTask
from researcher.schemas.review import ResearchQualityReviewOutput
from researcher.schemas.source import ResearchSource
from tests.researcher_helpers import valid_source


class ResearcherPayloadTests(unittest.TestCase):
    def test_valid_source_is_accepted(self):
        source = ResearchSource.model_validate(valid_source())
        self.assertEqual(source.source_type, "ACADEMIC")

    def test_empty_url_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(url=""))

    def test_missing_source_id_is_rejected(self):
        data = valid_source()
        data.pop("source_id")
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(data)

    def test_missing_evidence_id_is_rejected(self):
        data = valid_source()
        data.pop("evidence_id")
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(data)

    def test_invalid_source_type_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(source_type="BLOG"))

    def test_invalid_stance_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(stance="TRUE"))

    def test_naive_timestamp_is_rejected(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(retrieved_at="2026-08-01T12:00:00"))

    def test_question_ids_cannot_be_empty(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(research_question_ids=[]))

    def test_category_metadata_is_required(self):
        with self.assertRaises(ValidationError):
            ResearchSource.model_validate(valid_source(source_specific_metadata={}))

    def test_complete_result_requires_source(self):
        with self.assertRaises(ValidationError):
            ResearchResult.model_validate(
                {
                    "task_id": "task_1",
                    "research_question_id": "rq_employment",
                    "agent_id": "researcher.academic_researcher",
                    "sources": [],
                    "search_summary": "done",
                    "coverage_status": "COMPLETE",
                    "limitations": [],
                }
            )

    def test_source_must_trace_to_result_question(self):
        with self.assertRaises(ValidationError):
            ResearchResult.model_validate(
                {
                    "task_id": "task_1",
                    "research_question_id": "rq_other",
                    "agent_id": "researcher.academic_researcher",
                    "sources": [valid_source()],
                    "search_summary": "done",
                    "coverage_status": "COMPLETE",
                    "limitations": [],
                }
            )

    def test_task_target_must_match_agent(self):
        with self.assertRaises(ValidationError):
            ResearchTask.model_validate(
                {
                    "task_id": "task_1",
                    "research_question_id": "rq_1",
                    "target_agent_id": "researcher.news_researcher",
                    "research_target": "ACADEMIC",
                    "question": "question",
                    "scope": ["日本"],
                    "constraints": ["一次情報"],
                    "max_sources": 5,
                    "revision_context": None,
                }
            )

    def test_revision_review_requires_findings_and_targets(self):
        with self.assertRaises(ValidationError):
            ResearchQualityReviewOutput.model_validate(
                {
                    "status": "revision_required",
                    "reason": "missing",
                    "findings": [],
                    "revision_targets": [],
                    "approved_research_report": None,
                }
            )


if __name__ == "__main__":
    unittest.main()
