from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from common.models.pmp import PMPMessage
from common.models.revision import RevisionResultV1
from providers.mock_provider import MockModelProvider
from tests.conclusion_helpers import make_conclusion_manager
from tests.conclusion_helpers import make_conclusion_handoff
from tests.deliberation_helpers import make_manager as make_deliberation_manager
from tests.playwright_helpers import make_playwright_handoff, make_playwright_manager


class ConclusionPlaywrightRevisionRoundTripTests(unittest.TestCase):
    def test_conclusion_deliberation_conclusion_roundtrip_uses_canonical_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                conclusion_review_decisions=[
                    "upstream_revision_required",
                    "approved",
                ]
            )
            conclusion = make_conclusion_manager(data_dir, provider)
            waiting = asyncio.run(
                conclusion.start_from_message(
                    make_conclusion_handoff(data_dir, provider)
                )
            )
            self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
            deliberation = make_deliberation_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            revised_deliberation = asyncio.run(
                deliberation.revise(
                    waiting.workflow_id,
                    actor_id="test.operator",
                    actor_source="API",
                    reason="Test Conclusion-to-Deliberation roundtrip",
                )
            )
            self.assertEqual(revised_deliberation.status, "COMPLETED")
            resumed = asyncio.run(conclusion.resume(waiting.workflow_id))
            self.assertEqual(resumed.status, "WAITING_HUMAN_SELECTION")
            self.assertEqual(resumed.revision_control.phase, "completed")
            result_files = list(
                (
                    data_dir
                    / "outbox"
                    / "revision_results"
                    / "conclusion"
                    / waiting.workflow_id
                ).glob("*.json")
            )
            self.assertEqual(len(result_files), 1)
            result = RevisionResultV1.model_validate(
                PMPMessage.model_validate_json(
                    result_files[0].read_text(encoding="utf-8")
                ).payload
            )
            self.assertEqual(result.producer_layer, "deliberation")
            self.assertIn(
                "deliberation.deliberation_result",
                [item.artifact_type for item in result.result_artifacts],
            )

    def test_structural_revision_preserves_selection_and_resumes_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            malformed = handoff.model_dump(mode="json")
            malformed["message_id"] = str(uuid4())
            malformed["payload"]["traceability_manifest"]["evidence_ids"] = []
            playwright = make_playwright_manager(data_dir, provider)
            waiting = asyncio.run(
                playwright.start_from_message(PMPMessage.model_validate(malformed))
            )
            self.assertEqual(waiting.status, "WAITING_UPSTREAM_REVISION")
            old_selection = dict(waiting.human_selection)
            calls_before_conclusion = provider.calls

            conclusion = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            repaired = asyncio.run(conclusion.revise(waiting.workflow_id))

            self.assertEqual(repaired.status, "COMPLETED")
            self.assertEqual(repaired.revision_control.phase, "completed")
            self.assertEqual(repaired.human_selection, old_selection)
            self.assertEqual(provider.calls, calls_before_conclusion)
            resumed = asyncio.run(playwright.resume(waiting.workflow_id))
            self.assertEqual(resumed.status, "COMPLETED")
            self.assertTrue(resumed.delivered)
            self.assertEqual(resumed.human_selection, old_selection)
            result_files = list(
                (
                    data_dir
                    / "outbox"
                    / "revision_results"
                    / "playwright"
                    / waiting.workflow_id
                ).glob("*.json")
            )
            self.assertEqual(len(result_files), 1)
            result = RevisionResultV1.model_validate(
                PMPMessage.model_validate_json(
                    result_files[0].read_text(encoding="utf-8")
                ).payload
            )
            self.assertEqual(result.human_selection_impact, "unchanged")

    def test_semantic_revision_requires_new_human_selection_before_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            handoff = make_playwright_handoff(data_dir, provider)
            playwright = make_playwright_manager(data_dir, provider)
            completed = asyncio.run(playwright.start_from_message(handoff))
            self.assertEqual(completed.status, "COMPLETED")
            old_selection_id = completed.human_selection["selection_id"]
            request = {
                "revision_request_id": "pw_semantic_request_1",
                "final_conclusion_id": completed.final_conclusion[
                    "final_conclusion_id"
                ],
                "affected_claim_ids": list(
                    completed.final_conclusion.get("supporting_claim_ids") or []
                ),
                "affected_evidence_ids": list(
                    completed.final_conclusion.get("supporting_evidence_ids") or []
                ),
                "issue_type": "UNSUPPORTED_FINAL_CONCLUSION_CLAIM",
                "issue_description": "The selected recommendation needs semantic revision",
                "required_resolution": "Regenerate Conclusion candidates and reselect",
                "acceptance_conditions": [
                    "A new Human Selection is recorded before Playwright resumes"
                ],
                "source_finding_ids": ["pw_semantic_finding_1"],
            }
            waiting = asyncio.run(
                playwright._request_upstream_revision(completed, [request], None)
            )
            conclusion = make_conclusion_manager(
                data_dir,
                provider,
                demo_safe_mode=True,
            )
            reselection = asyncio.run(conclusion.revise(waiting.workflow_id))
            self.assertEqual(reselection.status, "WAITING_HUMAN_SELECTION")
            self.assertIsNone(reselection.human_selection)
            self.assertIsNone(reselection.final_conclusion)

            selected = conclusion.select(
                waiting.workflow_id,
                [reselection.position_candidates[0]["position_candidate_id"]],
            )
            self.assertEqual(selected.status, "COMPLETED")
            self.assertNotEqual(
                selected.human_selection["selection_id"],
                old_selection_id,
            )
            resumed = asyncio.run(playwright.resume(waiting.workflow_id))
            self.assertEqual(resumed.status, "COMPLETED")
            self.assertTrue(resumed.delivered)
            self.assertNotEqual(
                resumed.human_selection["selection_id"],
                old_selection_id,
            )
            delivery_files = list(
                (data_dir / "deliveries" / waiting.workflow_id).iterdir()
            )
            self.assertEqual(len(delivery_files), 6)


if __name__ == "__main__":
    unittest.main()
