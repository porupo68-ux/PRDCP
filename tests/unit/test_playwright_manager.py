import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from common.models.pmp import PMPMessage
from playwright.schemas import VisualPlan
from playwright.workflow import AGENT_ORDER
from providers.mock import playwright_fixtures
from providers.mock_provider import MockModelProvider
from tests.playwright_helpers import make_playwright_handoff, make_playwright_manager


class PersistentVisualErrorProvider(MockModelProvider):
    async def generate_structured(self, **kwargs):
        if kwargs["output_schema"] is VisualPlan:
            self.calls.append("VisualPlan")
            self._count_playwright_call("VisualPlan")
            return await self._playwright_result(
                kwargs["input_data"],
                lambda data: playwright_fixtures.visual_plan(data, mismatch=True),
            )
        return await super().generate_structured(**kwargs)


class PlaywrightManagerTests(unittest.TestCase):
    def test_normal_flow_delivers_six_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(handoff))
            self.assertEqual(state.status, "COMPLETED")
            self.assertTrue(state.delivered)
            self.assertEqual(set(state.delivery_paths), {
                "final_script_package", "script", "citation_manifest",
                "source_list", "visual_plan", "production_notes",
            })
            self.assertTrue(all(Path(path).exists() for path in state.delivery_paths.values()))
            self.assertEqual(state.message_history[-1].message_type, "final_script_delivery")
            self.assertEqual(state.message_history[-1].receiver_agent_id, "system.final_output")

    def test_agents_execute_in_required_sequence(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(calls, AGENT_ORDER)
            self.assertEqual(state.completed_agents, AGENT_ORDER)

    def test_start_is_idempotent_after_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            manager = make_playwright_manager(data_dir, provider)
            manager.repository.write_json_atomic(
                manager.repository.conclusion_outbox_dir / f"{handoff.workflow_id}.json",
                handoff.model_dump(mode="json"),
            )
            first = asyncio.run(manager.start(handoff.workflow_id))
            call_count = len(provider.calls)
            second = asyncio.run(manager.start(handoff.workflow_id))
            self.assertEqual(first.status, second.status)
            self.assertEqual(len(provider.calls), call_count)

    def test_human_selection_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            raw["payload"]["human_selection"] = {}
            state = asyncio.run(
                make_playwright_manager(data_dir, provider).start_from_message(
                    PMPMessage.model_validate(raw)
                )
            )
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.error["code"], "HUMAN_SELECTION_MISSING")
            self.assertFalse(state.delivered)

    def test_invalid_conclusion_routing_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            raw["sender_agent_id"] = "deliberation.manager"
            with self.assertRaises(ValueError):
                asyncio.run(
                    make_playwright_manager(data_dir, provider).start_from_message(
                        PMPMessage.model_validate(raw)
                    )
                )

    def test_incomplete_traceability_requests_conclusion_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            raw = make_playwright_handoff(data_dir, provider).model_dump(mode="json")
            raw["payload"]["traceability_manifest"]["claim_ids"] = []
            raw["message_id"] = str(uuid4())
            state = asyncio.run(
                make_playwright_manager(data_dir, provider).start_from_message(
                    PMPMessage.model_validate(raw)
                )
            )
            self.assertEqual(state.status, "WAITING_UPSTREAM_REVISION")
            path = data_dir / "outbox" / "conclusion_revision" / f"{state.workflow_id}.json"
            message = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "revision_request")
            self.assertEqual(message["receiver_agent_id"], "conclusion.manager")

    def test_upstream_revision_can_resume_with_new_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            good = make_playwright_handoff(data_dir, provider)
            bad = good.model_dump(mode="json")
            bad["message_id"] = str(uuid4())
            bad["payload"]["traceability_manifest"]["claim_ids"] = []
            manager = make_playwright_manager(data_dir, provider)
            waiting = asyncio.run(manager.start_from_message(PMPMessage.model_validate(bad)))
            self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
            resumed = asyncio.run(manager.resume(waiting.workflow_id))
            self.assertEqual(resumed.status, "COMPLETED")
            self.assertEqual(resumed.upstream_revision_count, 1)

    def test_agent_failure_fails_without_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(fail_agent_ids={"playwright.scriptwriter"})
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            self.assertEqual(state.status, "FAILED")
            self.assertIn("playwright.scriptwriter", state.failed_agents)
            self.assertFalse(state.delivered)

    def test_unsupported_claim_revision_reruns_script_and_dependents(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_unsupported_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.revision_count, 1)
            self.assertEqual(calls.count("playwright.narrative_architect"), 1)
            self.assertEqual(calls.count("playwright.scriptwriter"), 2)
            self.assertEqual(calls.count("playwright.evidence_citation_editor"), 2)
            self.assertEqual(calls.count("playwright.visual_director"), 2)
            revision_requests = [
                message for message in state.message_history
                if message.receiver_agent_id == "playwright.scriptwriter"
                and message.message_type == "revision_request"
            ]
            self.assertEqual(len(revision_requests), 1)

    def test_missing_citation_revision_starts_at_evidence_editor(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_missing_citation_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(calls.count("playwright.scriptwriter"), 1)
            self.assertEqual(calls.count("playwright.evidence_citation_editor"), 2)
            self.assertEqual(calls.count("playwright.visual_director"), 2)

    def test_visual_revision_reruns_only_visual_director(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_visual_mismatch_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            calls = [item for item in provider.agent_calls if item.startswith("playwright.")]
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(calls.count("playwright.evidence_citation_editor"), 1)
            self.assertEqual(calls.count("playwright.visual_director"), 2)

    def test_chart_without_source_is_revised(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(playwright_missing_chart_source_once=True)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.revision_count, 1)
            self.assertIn(state.final_gate_result["status"], {"APPROVED", "APPROVED_WITH_LIMITATIONS"})

    def test_two_failed_revisions_block_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = PersistentVisualErrorProvider()
            manager = make_playwright_manager(data_dir, provider, max_revisions=2)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            self.assertEqual(state.status, "BLOCKED")
            self.assertEqual(state.revision_count, 2)
            self.assertFalse(state.delivered)
            self.assertEqual(provider.agent_calls.count("playwright.visual_director"), 3)

    def test_rd_trace_is_recorded_for_manager_and_four_agents(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(make_playwright_handoff(data_dir, provider)))
            traces = state.role_definition_usage
            agent_ids = {item["agent_id"] for item in traces}
            hashes = {item["role_definition_hash"] for item in traces}
            self.assertEqual(agent_ids, {"playwright.manager", *AGENT_ORDER})
            self.assertEqual(len(hashes), 5)
            self.assertTrue(all(value.startswith("sha256:") for value in hashes))

    def test_final_conclusion_identity_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            manager = make_playwright_manager(data_dir, provider)
            state = asyncio.run(manager.start_from_message(handoff))
            package = manager.repository.load_final_package(state.workflow_id)
            self.assertEqual(
                package.final_conclusion_id,
                handoff.payload["final_conclusion"]["final_conclusion_id"],
            )
            self.assertEqual(
                package.human_selection_id,
                handoff.payload["human_selection"]["selection_id"],
            )

    def test_no_independent_playwright_quality_reviewer_is_registered(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = make_playwright_manager(Path(temporary), MockModelProvider())
            self.assertEqual(manager.registry.agent_ids, set(AGENT_ORDER))
            self.assertNotIn("playwright.quality_reviewer", manager.registry.agent_ids)


if __name__ == "__main__":
    unittest.main()
