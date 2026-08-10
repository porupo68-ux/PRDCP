import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from common.models.pmp import MessageType, PMPMessage
from deliberation.manager import DeliberationManager
from providers.mock_provider import MockModelProvider
from tests.deliberation_helpers import make_deliberation_handoff, make_manager, make_report


class DeliberationManagerTests(unittest.TestCase):
    def test_normal_flow_writes_result_and_conclusion_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            handoff = make_deliberation_handoff()
            state = asyncio.run(manager.start_from_message(handoff))
            self.assertEqual(state.status, "COMPLETED")
            self.assertTrue(state.conclusion_sent)
            self.assertTrue((Path(temporary) / "artifacts" / "deliberation_results" / f"{state.workflow_id}.json").exists())
            self.assertTrue((Path(temporary) / "outbox" / "conclusion" / f"{state.workflow_id}.json").exists())

    def test_primary_analysts_run_concurrently(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(delay_seconds=0.03)
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertGreaterEqual(provider.max_active_deliberation_calls, 3)

    def test_one_primary_failure_can_complete_with_conditions(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={"deliberation.stakeholder_response_analyst"}
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.review_result["status"], "approved_with_conditions")
            self.assertIn("deliberation.stakeholder_response_analyst", state.failed_agents)

    def test_any_single_primary_failure_can_complete_with_conditions(self):
        agent_ids = (
            "deliberation.argument_analyst",
            "deliberation.causal_structural_analyst",
            "deliberation.stakeholder_response_analyst",
        )
        for agent_id in agent_ids:
            with self.subTest(agent_id=agent_id), tempfile.TemporaryDirectory() as temporary:
                provider = MockModelProvider(fail_agent_ids={agent_id})
                manager = make_manager(Path(temporary), provider)
                state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
                self.assertEqual(state.status, "COMPLETED")
                self.assertEqual(state.review_result["status"], "approved_with_conditions")
                self.assertIn(agent_id, state.failed_agents)

    def test_two_primary_failures_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={
                    "deliberation.causal_structural_analyst",
                    "deliberation.stakeholder_response_analyst",
                }
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "BLOCKED")
            self.assertFalse(state.conclusion_sent)

    def test_all_primary_failures_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={
                    "deliberation.argument_analyst",
                    "deliberation.causal_structural_analyst",
                    "deliberation.stakeholder_response_analyst",
                }
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "FAILED")

    def test_counterargument_failure_fails_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                fail_agent_ids={"deliberation.counterargument_analyst"}
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "FAILED")
            self.assertFalse(state.conclusion_sent)

    def test_quality_reviewer_failure_fails_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(fail_schemas={"DeliberationQualityReviewOutput"})
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "FAILED")

    def test_revision_reruns_argument_and_all_downstream_stages(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "approved"]
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.revision_count, 1)
            self.assertEqual(provider.agent_calls.count("deliberation.argument_analyst"), 2)
            self.assertEqual(provider.agent_calls.count("deliberation.causal_structural_analyst"), 1)
            self.assertEqual(provider.agent_calls.count("deliberation.stakeholder_response_analyst"), 1)
            self.assertEqual(provider.agent_calls.count("deliberation.counterargument_analyst"), 2)

    def test_two_revision_required_reviews_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["revision_required", "revision_required"]
            )
            manager = make_manager(Path(temporary), provider, max_revisions=2)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 2)
            self.assertFalse(state.conclusion_sent)

    def test_upstream_evidence_request_waits_and_writes_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["upstream_evidence_required"]
            )
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "WAITING_UPSTREAM_REVISION")
            path = Path(temporary) / "outbox" / "researcher_revision" / f"{state.workflow_id}.json"
            self.assertTrue(path.exists())
            message = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "research_revision_request")

    def test_upstream_revision_can_resume_with_new_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider(
                deliberation_review_decisions=["upstream_evidence_required", "approved"]
            )
            manager = make_manager(Path(temporary), provider)
            initial = make_deliberation_handoff()
            waiting = asyncio.run(manager.start_from_message(initial))
            revised = make_deliberation_handoff(
                make_report(waiting.workflow_id),
                message_type=MessageType.RESEARCH_REVISION_RESULT,
            )
            manager.repository.write_json_atomic(
                manager.repository.researcher_outbox_dir / f"{waiting.workflow_id}.json",
                revised.model_dump(mode="json"),
            )
            completed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(completed.status, "COMPLETED")
            self.assertEqual(completed.upstream_revision_count, 1)

    def test_unapproved_research_report_is_rejected_before_state_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            handoff = make_deliberation_handoff(
                make_report(review_status="revision_required")
            )
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(handoff))

    def test_invalid_researcher_routing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            handoff = make_deliberation_handoff()
            raw = handoff.model_dump(mode="json")
            raw["sender_agent_id"] = "producer.manager"
            invalid = PMPMessage.model_validate(raw)
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(invalid))

    def test_large_evidence_set_is_bounded_per_task(self):
        report = make_report(evidence_count=51)
        tasks = DeliberationManager._create_analysis_tasks(report)
        self.assertEqual(len(tasks), 3)
        self.assertTrue(all(len(task.target_evidence_ids) <= 50 for task in tasks))

    def test_conclusion_handoff_contains_canonical_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_manager(Path(temporary))
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            path = Path(temporary) / "outbox" / "conclusion" / f"{state.workflow_id}.json"
            payload = json.loads(path.read_text(encoding="utf-8"))["payload"]
            required = {
                "problem_definition",
                "claim_structure",
                "key_assumptions",
                "evidence_relationships",
                "causal_model",
                "structural_factors",
                "stakeholder_structure",
                "existing_response_evaluation",
                "counterarguments",
                "alternative_interpretations",
                "trade_offs",
                "uncertainties",
                "analysis_perspectives",
                "unresolved_issues",
                "research_gaps",
                "source_traceability",
                "quality_review",
            }
            self.assertFalse(required - payload.keys())
            self.assertLessEqual(len(payload["analysis_perspectives"]), 3)

    def test_workflow_sequence_places_counterargument_after_initial_integration(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            manager = make_manager(Path(temporary), provider)
            state = asyncio.run(manager.start_from_message(make_deliberation_handoff()))
            self.assertEqual(state.status, "COMPLETED")
            self.assertLess(provider.calls.index("InitialIntegratedAnalysis"), provider.calls.index("CounterargumentAnalysisResult"))
            self.assertLess(provider.calls.index("CounterargumentAnalysisResult"), provider.calls.index("FinalIntegratedAnalysis"))
            self.assertLess(provider.calls.index("FinalIntegratedAnalysis"), provider.calls.index("DeliberationQualityReviewOutput"))

    def test_start_is_idempotent_for_saved_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = MockModelProvider()
            manager = make_manager(Path(temporary), provider)
            handoff = make_deliberation_handoff()
            manager.repository.write_json_atomic(
                manager.repository.researcher_outbox_dir / f"{handoff.workflow_id}.json",
                handoff.model_dump(mode="json"),
            )
            first = asyncio.run(manager.start(handoff.workflow_id))
            calls = len(provider.calls)
            second = asyncio.run(manager.start(handoff.workflow_id))
            self.assertEqual(first.status, second.status)
            self.assertEqual(len(provider.calls), calls)


if __name__ == "__main__":
    unittest.main()
