import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from common.models.pmp import PMPMessage
from providers.mock_provider import MockModelProvider
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.source import ResearchSource
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from tests.researcher_helpers import make_handoff, valid_source


class ResearcherManagerTests(unittest.TestCase):
    def run_manager(self, provider: MockModelProvider, *, max_revisions: int = 3):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = ResearcherWorkflowRepository(Path(temporary.name))
        manager = ResearcherManager(
            ResearcherRegistry(provider),
            repository,
            max_revisions=max_revisions,
        )
        state = asyncio.run(manager.start_from_message(make_handoff()))
        return state, repository, manager

    def test_normal_flow_writes_report_and_deliberation_outbox(self):
        state, repository, _manager = self.run_manager(MockModelProvider())
        self.assertEqual(state.status, "COMPLETED")
        self.assertTrue(state.deliberation_sent)
        self.assertEqual(len(state.research_tasks), 7)
        self.assertEqual(len(state.collected_sources), 7)
        self.assertTrue((repository.reports_dir / f"{state.workflow_id}.json").exists())
        outbox = repository.deliberation_outbox_dir / f"{state.workflow_id}.json"
        self.assertTrue(outbox.exists())
        payload = json.loads(outbox.read_text(encoding="utf-8"))
        self.assertEqual(payload["message_type"], "research_result")
        self.assertEqual(payload["receiver_agent_id"], "deliberation.manager")
        self.assertIn("evidence_items", payload["payload"])

    def test_specialists_run_concurrently(self):
        provider = MockModelProvider(delay_seconds=0.02)
        state, _repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertGreater(provider.max_active_research_calls, 1)

    def test_partial_failure_is_disclosed_and_can_complete_with_conditions(self):
        provider = MockModelProvider(
            fail_agent_ids={"researcher.public_opinion_researcher"}
        )
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(state.review_result["status"], "approved_with_conditions")
        self.assertTrue(state.limitations)
        self.assertTrue(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_no_result_is_preserved_as_gap_and_limitation(self):
        provider = MockModelProvider(
            no_result_agent_ids={"researcher.government_researcher"},
            researcher_review_decisions=["approved_with_conditions"],
        )
        state, _repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        report = state.research_report
        self.assertTrue(report["evidence_gaps"])
        self.assertTrue(report["unresolved_questions"])
        self.assertTrue(report["research_limitations"])

    def test_all_specialists_failing_aborts_without_handoff(self):
        provider = MockModelProvider(
            fail_agent_ids={
                "researcher.expert_researcher",
                "researcher.academic_researcher",
                "researcher.government_researcher",
                "researcher.news_researcher",
                "researcher.public_opinion_researcher",
                "researcher.politician_researcher",
                "researcher.industry_researcher",
            }
        )
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "FAILED")
        self.assertFalse(state.deliberation_sent)
        self.assertFalse(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_revision_reruns_only_target_agent(self):
        provider = MockModelProvider(
            researcher_review_decisions=["revision_required", "approved"]
        )
        state, _repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(state.revision_count, 1)
        self.assertEqual(provider.agent_calls.count("researcher.government_researcher"), 2)
        self.assertEqual(provider.agent_calls.count("researcher.academic_researcher"), 1)
        self.assertTrue(
            any(
                message.message_type == "research_revision_request"
                for message in state.message_history
            )
        )

    def test_three_revision_required_reviews_block(self):
        provider = MockModelProvider(
            researcher_review_decisions=["revision_required"] * 3
        )
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.revision_count, 3)
        self.assertFalse(state.deliberation_sent)
        self.assertFalse(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_quality_reviewer_error_fails_safely(self):
        provider = MockModelProvider(fail_schemas={"ResearchQualityReviewOutput"})
        state, repository, _manager = self.run_manager(provider)
        self.assertEqual(state.status, "FAILED")
        self.assertIn("Quality Reviewer", state.error["message"])
        self.assertFalse(
            (repository.deliberation_outbox_dir / f"{state.workflow_id}.json").exists()
        )

    def test_duplicate_url_merges_question_ids(self):
        _state, _repository, manager = self.run_manager(MockModelProvider())
        first = ResearchSource.model_validate(valid_source())
        second_data = valid_source(
            source_id="source_2",
            evidence_id="evidence_2",
            research_question_ids=["rq_views"],
            url="https://example.invalid/source?tracking=1",
        )
        second = ResearchSource.model_validate(second_data)
        merged = manager._deduplicate_sources([first, second])
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].research_question_ids), {"rq_employment", "rq_views"})

    def test_invalid_parent_message_is_rejected(self):
        provider = MockModelProvider()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = ResearcherWorkflowRepository(Path(temporary.name))
        registry = ResearcherRegistry(provider)
        original = registry.get("researcher.academic_researcher")

        class InvalidAgent:
            async def execute(self, request):
                response = await original.execute(request)
                data = response.model_dump()
                data["parent_message_id"] = str(__import__("uuid").uuid4())
                return PMPMessage.model_validate(data)

        registry._agents["researcher.academic_researcher"] = InvalidAgent()
        manager = ResearcherManager(registry, repository)
        state = asyncio.run(manager.start_from_message(make_handoff()))
        self.assertEqual(state.status, "COMPLETED")
        self.assertIn("researcher.academic_researcher", state.failed_agents)
        self.assertEqual(state.review_result["status"], "approved_with_conditions")

    def test_invalid_producer_handoff_routing_is_rejected(self):
        provider = MockModelProvider()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = ResearcherWorkflowRepository(Path(temporary.name))
        manager = ResearcherManager(ResearcherRegistry(provider), repository)
        handoff = make_handoff()
        data = handoff.model_dump()
        data["receiver_agent_id"] = "producer.topic_scout"
        invalid = PMPMessage.model_validate(data)
        with self.assertRaises(ValueError):
            asyncio.run(manager.start_from_message(invalid))


if __name__ == "__main__":
    unittest.main()
