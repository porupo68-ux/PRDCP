import asyncio
import tempfile
import unittest
from pathlib import Path

from producer.manager import ProducerManager
from producer.registry import ProducerRegistry
from producer.schemas.review import QualityReviewOutput
from providers.mock_provider import MockModelProvider
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.review import ResearchQualityReviewOutput
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from storage.workflow_repository import WorkflowRepository


class UpstreamPlanDefectProvider(MockModelProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._upstream_review_pending = True

    async def generate_structured(self, **kwargs):
        if (
            kwargs["output_schema"] is ResearchQualityReviewOutput
            and self._upstream_review_pending
        ):
            self.calls.append("ResearchQualityReviewOutput")
            self._upstream_review_pending = False
            report = kwargs["input_data"]["research_report"]
            return {
                "status": "revision_required",
                "reason": "The Research Plan category assignment is incomplete",
                "findings": [
                    {
                        "finding_id": "research_plan_defect_001",
                        "finding_type": "UPSTREAM_PLAN_DEFECT",
                        "severity": "MAJOR",
                        "research_question_id": report["research_questions"][0][
                            "research_question_id"
                        ],
                        "target_agent_id": "producer.research_planner",
                        "issue": "The approved plan lacks the required category split",
                        "required_action": "Revise plan scope and category assignment",
                    }
                ],
                "revision_targets": [],
                "approved_research_report": None,
            }
        return await super().generate_structured(**kwargs)


class ResearcherProducerRevisionRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)

    def test_plan_defect_round_trip_is_correlated_and_separately_authorized(self):
        producer_provider = MockModelProvider(
            reservation_root=self.data_dir / "provider_call_reservations"
        )
        producer_repository = WorkflowRepository(self.data_dir)
        producer = ProducerManager(
            ProducerRegistry(producer_provider, demo_safe_mode=True),
            producer_repository,
            demo_safe_mode=True,
        )
        producer_state = asyncio.run(producer.start(user_topic="AI and work"))
        self.assertEqual(producer_state.status, "COMPLETED")

        researcher_provider = UpstreamPlanDefectProvider(
            reservation_root=self.data_dir / "provider_call_reservations"
        )
        researcher_repository = ResearcherWorkflowRepository(self.data_dir)
        researcher = ResearcherManager(
            ResearcherRegistry(researcher_provider, demo_safe_mode=True),
            researcher_repository,
            demo_safe_mode=True,
        )
        handoff = researcher_repository.load_producer_handoff(
            producer_state.workflow_id
        )
        waiting = asyncio.run(researcher.start_from_message(handoff))
        self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
        self.assertEqual(waiting.revision_control.phase, "waiting_upstream_result")
        upstream_request_id = waiting.revision_control.active_request_id
        self.assertIsNotNone(upstream_request_id)
        request = researcher.revision_exchange.load_request(
            target_layer="producer",
            workflow_id=waiting.workflow_id,
            revision_request_id=upstream_request_id,
        )
        self.assertEqual(request.payload["target_agent_ids"], ["producer.research_planner"])
        self.assertFalse(request.payload["retrieval_allowed"])

        producer_calls_before = len(producer_provider.calls)
        revised_producer = asyncio.run(
            producer.revise(
                waiting.workflow_id,
                actor_id="test.operator",
                actor_source="CLI",
                reason="Authorize the bounded Research Plan repair",
            )
        )
        self.assertEqual(revised_producer.status, "COMPLETED")
        self.assertEqual(len(producer_provider.calls) - producer_calls_before, 2)
        self.assertEqual(
            producer_provider.calls[-2:],
            ["ResearchPlanOutput", "QualityReviewOutput"],
        )
        result = researcher.revision_exchange.load_result(
            requester_layer="researcher",
            workflow_id=waiting.workflow_id,
            revision_request_id=upstream_request_id,
            request_message=request,
        )
        self.assertEqual(result.payload["status"], "completed")

        researcher_calls_before = len(researcher_provider.calls)
        refresh_waiting = asyncio.run(researcher.resume(waiting.workflow_id))
        self.assertEqual(refresh_waiting.status, "BLOCKED")
        self.assertEqual(
            refresh_waiting.revision_control.phase,
            "authorization_required",
        )
        self.assertEqual(len(researcher_provider.calls), researcher_calls_before)
        child_request_id = refresh_waiting.revision_control.active_request_id
        self.assertNotEqual(child_request_id, upstream_request_id)
        self.assertIn(
            upstream_request_id,
            refresh_waiting.revision_control.consumed_request_ids,
        )

        refreshed = asyncio.run(
            researcher.execute_authorized_revision(
                waiting.workflow_id,
                actor_id="test.operator",
                actor_source="CLI",
                authorization_reason="Authorize only affected Researcher refresh tasks",
            )
        )
        self.assertEqual(refreshed.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
        self.assertEqual(refreshed.revision_control.phase, "completed")
        self.assertIn(child_request_id, refreshed.revision_control.consumed_request_ids)
        self.assertGreater(len(researcher_provider.calls), researcher_calls_before)

    def test_upstream_result_consumer_rejects_stale_research_plan(self):
        producer_provider = MockModelProvider(
            reservation_root=self.data_dir / "provider_call_reservations"
        )
        producer_repository = WorkflowRepository(self.data_dir)
        producer = ProducerManager(
            ProducerRegistry(producer_provider, demo_safe_mode=True),
            producer_repository,
            demo_safe_mode=True,
        )
        producer_state = asyncio.run(producer.start(user_topic="AI and work"))
        researcher_provider = UpstreamPlanDefectProvider(
            reservation_root=self.data_dir / "provider_call_reservations"
        )
        repository = ResearcherWorkflowRepository(self.data_dir)
        researcher = ResearcherManager(
            ResearcherRegistry(researcher_provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )
        waiting = asyncio.run(
            researcher.start_from_message(
                repository.load_producer_handoff(producer_state.workflow_id)
            )
        )
        asyncio.run(
            producer.revise(
                waiting.workflow_id,
                actor_id="test.operator",
                actor_source="CLI",
                reason="Authorize plan repair",
            )
        )
        stale = repository.load(waiting.workflow_id)
        stale.research_plan["scope"].append("unaudited local mutation")
        repository.save(stale)
        with self.assertRaisesRegex(ValueError, "stale"):
            asyncio.run(researcher.resume(waiting.workflow_id))


if __name__ == "__main__":
    unittest.main()
