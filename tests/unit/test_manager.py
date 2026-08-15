import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from common.models.pmp import MessageType, PMPMessage
from producer.manager import ProducerManager
from producer.registry import ProducerRegistry
from providers.mock_provider import MockModelProvider
from storage.workflow_repository import WorkflowRepository


class ProducerManagerTests(unittest.TestCase):
    def run_workflow(self, provider: MockModelProvider):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = WorkflowRepository(Path(temporary.name))
        manager = ProducerManager(
            ProducerRegistry(provider, demo_safe_mode=False),
            repository,
            demo_safe_mode=False,
        )
        state = asyncio.run(manager.start(user_topic="生成AIは人間の仕事を奪うのか"))
        return state, repository

    def test_normal_transition_completes_and_writes_outbox(self):
        state, repository = self.run_workflow(MockModelProvider())
        self.assertEqual(state.status, "COMPLETED")
        self.assertTrue(state.researcher_sent)
        self.assertEqual(len(state.completed_agents), 5)
        outbox = repository.researcher_outbox_dir / f"{state.workflow_id}.json"
        self.assertTrue(outbox.exists())
        payload = json.loads(outbox.read_text(encoding="utf-8"))
        self.assertEqual(payload["message_type"], "research_plan")
        self.assertEqual(payload["receiver_agent_id"], "researcher.manager")

    def test_reject_reruns_from_target_then_approves(self):
        provider = MockModelProvider(review_decisions=["revision_required", "approved"])
        state, _repository = self.run_workflow(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(state.revision_count, 1)
        self.assertEqual(provider.calls.count("ResearchPlanOutput"), 2)
        self.assertEqual(provider.calls.count("QualityReviewOutput"), 2)
        self.assertIn("産業別・職種別の差を区別する", state.research_plan["scope"])
        self.assertTrue(any(m.message_type == "revision_request" for m in state.message_history))

    def test_three_rejections_fail(self):
        provider = MockModelProvider(review_decisions=["revision_required"] * 3)
        state, repository = self.run_workflow(provider)
        self.assertEqual(state.status, "FAILED")
        self.assertEqual(state.revision_count, 3)
        self.assertFalse(state.researcher_sent)
        self.assertFalse((repository.researcher_outbox_dir / f"{state.workflow_id}.json").exists())

    def test_agent_error_aborts_workflow(self):
        provider = MockModelProvider(fail_schemas={"TopicSelectorOutput"})
        state, _repository = self.run_workflow(provider)
        self.assertEqual(state.status, "FAILED")
        self.assertIn("unclassified provider error", state.error["message"])
        self.assertEqual(provider.calls.count("TopicSelectorOutput"), 1)

    def test_invalid_agent_response_is_rejected(self):
        provider = MockModelProvider()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = WorkflowRepository(Path(temporary.name))
        registry = ProducerRegistry(provider, demo_safe_mode=False)
        original_agent = registry.get("producer.topic_scout")

        class InvalidResponseAgent:
            async def execute(self, request):
                response = await original_agent.execute(request)
                data = response.model_dump()
                data["parent_message_id"] = str(__import__("uuid").uuid4())
                return PMPMessage.model_validate(data)

        registry._agents["producer.topic_scout"] = InvalidResponseAgent()
        manager = ProducerManager(registry, repository, demo_safe_mode=False)
        state = asyncio.run(manager.start())
        self.assertEqual(state.status, "FAILED")
        self.assertIn("Parent message ID mismatch", state.error["message"])


if __name__ == "__main__":
    unittest.main()
