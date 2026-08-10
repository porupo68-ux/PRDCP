import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from common.models.pmp import PMPMessage
from providers.mock_provider import MockModelProvider
from tests.conclusion_helpers import make_conclusion_handoff, make_conclusion_manager


class ConclusionManagerTests(unittest.TestCase):
    def test_normal_flow_waits_for_human_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_conclusion_handoff(data_dir, provider)
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(handoff))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(len(state.position_candidates), 3)
            self.assertFalse(state.playwright_sent)
            self.assertTrue((data_dir / "artifacts" / "conclusion_packages" / f"{state.workflow_id}.json").exists())
            self.assertFalse((data_dir / "outbox" / "playwright" / f"{state.workflow_id}.json").exists())

    def test_human_selection_finalizes_and_writes_playwright_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            selected_id = state.position_candidates[0]["position_candidate_id"]
            final = manager.select(state.workflow_id, [selected_id])
            self.assertEqual(final.status, "COMPLETED")
            self.assertTrue(final.playwright_sent)
            self.assertEqual(final.human_selection["selected_candidate_ids"], [selected_id])
            outbox = data_dir / "outbox" / "playwright" / f"{state.workflow_id}.json"
            message = json.loads(outbox.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "conclusion_handoff")
            self.assertEqual(message["receiver_agent_id"], "playwright.manager")

    def test_invalid_candidate_selection_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            with self.assertRaises(ValueError):
                manager.select(state.workflow_id, ["position_missing"])

    def test_final_selection_is_idempotent_and_immutable(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            first = manager.select(waiting.workflow_id, [waiting.position_candidates[0]["position_candidate_id"]])
            second = manager.select(waiting.workflow_id, [waiting.position_candidates[1]["position_candidate_id"]])
            self.assertEqual(first.final_conclusion, second.final_conclusion)

    def test_duplicate_candidate_revision_reruns_all_dependents(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_duplicate_candidates_once=True,
                conclusion_review_decisions=["revision_required", "approved"],
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(state.revision_count, 1)
            self.assertEqual(provider.agent_calls.count("conclusion.position_generator"), 2)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_evaluator"), 2)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_integrator"), 2)

    def test_evaluator_revision_does_not_rerun_position_generator(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["evaluator_revision_required", "approved"]
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(provider.agent_calls.count("conclusion.position_generator"), 1)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_evaluator"), 2)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_integrator"), 2)

    def test_two_revision_required_reviews_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["revision_required", "revision_required"]
            )
            manager = make_conclusion_manager(data_dir, provider, max_revisions=2)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 2)
            self.assertFalse(state.playwright_sent)

    def test_upstream_revision_waits_and_writes_deliberation_outbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["upstream_revision_required"]
            )
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_UPSTREAM_REVISION")
            path = data_dir / "outbox" / "deliberation_revision" / f"{state.workflow_id}.json"
            self.assertTrue(path.exists())
            message = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "revision_request")
            self.assertEqual(message["receiver_agent_id"], "deliberation.manager")

    def test_upstream_revision_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=["upstream_revision_required", "approved"]
            )
            handoff = make_conclusion_handoff(data_dir, provider)
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(handoff))
            revised = handoff.model_dump(mode="json")
            revised["message_id"] = str(uuid4())
            manager.repository.write_json_atomic(
                manager.repository.deliberation_outbox_dir / f"{waiting.workflow_id}.json",
                revised,
            )
            resumed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(resumed.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(resumed.upstream_revision_count, 1)

    def test_blocking_issue_is_not_compensated(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(conclusion_blocking_candidate_id="position_c")
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "WAITING_HUMAN_SELECTION")
            self.assertNotIn("position_c", state.decision_integration["viable_candidates"])
            self.assertIn("position_c", [item["candidate_id"] for item in state.decision_integration["excluded_candidates"]])

    def test_not_evaluable_is_not_zeroed(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            ratings = [
                item["rating"]
                for item in state.decision_evaluation["candidate_evaluations"]
                if item["candidate_id"] == "position_c" and item["criterion"] == "POLITICAL_FEASIBILITY"
            ]
            self.assertEqual(ratings, ["NOT_EVALUABLE"])

    def test_requested_candidate_integration_is_reviewed_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            candidate_ids = [item["position_candidate_id"] for item in waiting.position_candidates[:2]]
            updated = asyncio.run(manager.integrate_candidates(waiting.workflow_id, candidate_ids))
            self.assertEqual(updated.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(updated.decision_integration["integrated_option"]["candidate_ids"], candidate_ids)
            self.assertEqual(provider.agent_calls.count("conclusion.position_generator"), 1)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_evaluator"), 1)
            self.assertEqual(provider.agent_calls.count("conclusion.decision_integrator"), 2)

    def test_quality_reviewer_failure_fails_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(fail_agent_ids={"conclusion.quality_reviewer"})
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            self.assertEqual(state.status, "FAILED")
            self.assertFalse(state.playwright_sent)

    def test_unapproved_deliberation_handoff_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            handoff = make_conclusion_handoff(data_dir, provider)
            raw = handoff.model_dump(mode="json")
            raw["payload"]["quality_review"]["status"] = "revision_required"
            raw["payload"]["deliberation_result"]["quality_review"]["status"] = "revision_required"
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(PMPMessage.model_validate(raw)))

    def test_invalid_deliberation_routing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            handoff = make_conclusion_handoff(data_dir, provider)
            raw = handoff.model_dump(mode="json")
            raw["sender_agent_id"] = "researcher.manager"
            with self.assertRaises(ValueError):
                asyncio.run(manager.start_from_message(PMPMessage.model_validate(raw)))

    def test_start_is_idempotent_for_saved_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_conclusion_handoff(data_dir, provider)
            manager = make_conclusion_manager(data_dir, provider)
            manager.repository.write_json_atomic(
                manager.repository.deliberation_outbox_dir / f"{handoff.workflow_id}.json",
                handoff.model_dump(mode="json"),
            )
            first = asyncio.run(manager.start(handoff.workflow_id))
            call_count = len(provider.calls)
            second = asyncio.run(manager.start(handoff.workflow_id))
            self.assertEqual(first.status, second.status)
            self.assertEqual(len(provider.calls), call_count)

    def test_every_conclusion_agent_response_has_distinct_rd_trace(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            traces = []
            for message in state.message_history:
                if message.sender_agent_id.startswith("conclusion."):
                    trace = message.metadata.extensions.get("role_definition")
                    if trace:
                        traces.append(trace)
            agent_ids = {item["agent_id"] for item in traces}
            hashes = {item["role_definition_hash"] for item in traces}
            self.assertTrue({
                "conclusion.manager",
                "conclusion.position_generator",
                "conclusion.decision_evaluator",
                "conclusion.decision_integrator",
                "conclusion.quality_reviewer",
            } - agent_ids == set())
            self.assertGreaterEqual(len(hashes), 5)

    def test_playwright_handoff_contains_canonical_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(make_conclusion_handoff(data_dir, provider)))
            completed = manager.select(waiting.workflow_id, [waiting.position_candidates[0]["position_candidate_id"]])
            message = json.loads(
                (data_dir / "outbox" / "playwright" / f"{completed.workflow_id}.json").read_text(encoding="utf-8")
            )
            required = {
                "conclusion_id", "topic", "general_opinion", "central_question", "selected_position",
                "recommendations", "decision_rationale", "supporting_claims", "supporting_analysis",
                "evidence_links", "evaluation_summary", "implementation_conditions", "expected_benefits",
                "risks", "trade_offs", "affected_stakeholders", "counterarguments", "uncertainties",
                "limitations", "unresolved_issues", "prohibited_interpretations", "source_registry_reference",
                "quality_review", "workflow_metadata",
            }
            self.assertFalse(required - message["payload"].keys())
            self.assertEqual(message["payload"]["workflow_metadata"]["human_selection"]["selection_type"], "candidate")


if __name__ == "__main__":
    unittest.main()
