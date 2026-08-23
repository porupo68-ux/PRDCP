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

    def test_revision_budget_exhaustion_blocks_before_a_fourth_execution(self):
        provider = MockModelProvider(review_decisions=["revision_required"] * 4)
        state, repository = self.run_workflow(provider)
        self.assertEqual(state.status, "BLOCKED")
        self.assertEqual(state.revision_count, 3)
        self.assertIn("budget 3 is exhausted", state.error["message"])
        self.assertEqual(provider.calls.count("ResearchPlanOutput"), 4)
        self.assertEqual(provider.calls.count("QualityReviewOutput"), 4)
        self.assertFalse(state.researcher_sent)
        self.assertFalse((repository.researcher_outbox_dir / f"{state.workflow_id}.json").exists())

    def test_internal_revision_persists_correlated_request_result_and_unique_tasks(self):
        provider = MockModelProvider(review_decisions=["revision_required", "approved"])
        state, repository = self.run_workflow(provider)

        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(state.revision_control.phase, "completed")
        self.assertEqual(len(state.revision_control.consumed_request_ids), 1)
        request_id = state.revision_control.consumed_request_ids[0]
        request_path = (
            repository.data_dir
            / "artifacts"
            / "revision_requests"
            / "internal"
            / "producer"
            / state.workflow_id
            / f"{request_id}.json"
        )
        result_path = (
            repository.data_dir
            / "artifacts"
            / "revision_results"
            / "internal"
            / "producer"
            / state.workflow_id
            / f"{request_id}.json"
        )
        self.assertTrue(request_path.exists())
        self.assertTrue(result_path.exists())
        revision_tasks = [
            message.metadata.extensions.get("provider_task_id")
            for message in state.message_history
            if message.receiver_agent_id
            in {"producer.research_planner", "producer.quality_reviewer"}
            and message.metadata.extensions.get("provider_task_id")
        ]
        self.assertEqual(len(revision_tasks), 2)
        self.assertEqual(len(revision_tasks), len(set(revision_tasks)))
        self.assertTrue(all(".revision.1." in item for item in revision_tasks))

    def test_revision_crash_recovers_from_last_checkpoint_with_same_task_identity(self):
        provider = MockModelProvider(review_decisions=["revision_required", "approved"])
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repository = WorkflowRepository(Path(temporary.name))
        registry = ProducerRegistry(provider, demo_safe_mode=False)
        reviewer = registry.get("producer.quality_reviewer")

        class CrashBeforeSecondReview:
            def __init__(self):
                self.calls = 0

            async def execute(self, request):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("fault injection before Provider reservation")
                return await reviewer.execute(request)

        crashing = CrashBeforeSecondReview()
        registry._agents["producer.quality_reviewer"] = crashing
        manager = ProducerManager(registry, repository, demo_safe_mode=False)
        with self.assertRaisesRegex(RuntimeError, "fault injection"):
            asyncio.run(manager.start(user_topic="checkpoint recovery"))
        workflow_id = next(repository.workflows_dir.glob("*.json")).stem
        saved = repository.load(workflow_id)
        self.assertEqual(saved.revision_control.phase, "executing")
        self.assertIn("producer.research_planner", saved.completed_agents)
        self.assertNotIn("producer.quality_reviewer", saved.completed_agents)

        registry._agents["producer.quality_reviewer"] = reviewer
        recovered = asyncio.run(manager.recover(workflow_id))
        self.assertEqual(recovered.status, "COMPLETED")
        self.assertEqual(recovered.revision_control.phase, "completed")
        revision_review_requests = [
            message
            for message in recovered.message_history
            if message.receiver_agent_id == "producer.quality_reviewer"
            and message.metadata.extensions.get("provider_task_id")
        ]
        self.assertEqual(len(revision_review_requests), 2)
        self.assertEqual(
            revision_review_requests[0].metadata.extensions["provider_task_id"],
            revision_review_requests[1].metadata.extensions["provider_task_id"],
        )
        self.assertEqual(provider.calls.count("ResearchPlanOutput"), 2)
        self.assertEqual(provider.calls.count("QualityReviewOutput"), 2)

    def test_general_opinion_revision_authorizes_distinct_retrieval_identity(self):
        class GeneralOpinionRevisionProvider(MockModelProvider):
            async def generate_structured(self, **kwargs):
                payload = await super().generate_structured(**kwargs)
                if (
                    kwargs["output_schema"].__name__ == "QualityReviewOutput"
                    and payload["status"] == "revision_required"
                ):
                    payload["revision_target"] = "producer.general_opinion_analyst"
                return payload

        provider = GeneralOpinionRevisionProvider(
            review_decisions=["revision_required", "approved"]
        )
        state, repository = self.run_workflow(provider)
        self.assertEqual(state.status, "COMPLETED")
        self.assertEqual(
            state.revision_history[0].target_agent,
            "producer.general_opinion_analyst",
        )
        request_id = state.revision_control.consumed_request_ids[0]
        authorization_path = (
            repository.data_dir
            / "revision_authorizations"
            / "producer"
            / state.workflow_id
            / f"{request_id}.json"
        )
        authorization = json.loads(authorization_path.read_text(encoding="utf-8"))
        self.assertEqual(authorization["status"], "consumed")
        self.assertEqual(len(authorization["retrieval_reservation_ids"]), 1)
        retrieval_id = authorization["retrieval_reservation_ids"][0]
        reservation = (
            repository.data_dir
            / "retrieval_call_reservations"
            / "mock"
            / state.workflow_id
            / f"{retrieval_id}.json"
        )
        self.assertTrue(reservation.exists())
        self.assertEqual(provider.calls.count("GeneralOpinionOutput"), 2)

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
