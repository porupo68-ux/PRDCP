from __future__ import annotations

import contextlib
import io
import unittest

from cli_app.arguments import parse_args
from cli_app.output import format_state_summary
from common.agents import StructuredAgent
from common.specifications import audit_common_specifications
from conclusion.agents.base import ConclusionAgent
from deliberation.agents.base import DeliberationAgent
from playwright.agents.base import PlaywrightAgent
from producer.agents.base import ProducerAgent
from researcher.agents.base import ResearcherAgent


class MaintainabilityTests(unittest.TestCase):
    def test_common_specifications_match_runtime(self) -> None:
        checks = audit_common_specifications()
        failures = [check for check in checks if not check.passed]
        self.assertEqual([], failures)

    def test_all_layer_agents_use_one_shared_execution_pipeline(self) -> None:
        for layer_base in (
            ProducerAgent,
            ResearcherAgent,
            DeliberationAgent,
            ConclusionAgent,
            PlaywrightAgent,
        ):
            with self.subTest(layer_base=layer_base.__name__):
                self.assertTrue(issubclass(layer_base, StructuredAgent))
                self.assertNotIn("execute", layer_base.__dict__)
                self.assertNotIn("run", layer_base.__dict__)

    def test_every_workflow_exports_standard_discovery_fields(self) -> None:
        from conclusion import workflow as conclusion_workflow
        from deliberation import workflow as deliberation_workflow
        from playwright import workflow as playwright_workflow
        from producer import workflow as producer_workflow
        from researcher import workflow as researcher_workflow

        for module in (
            producer_workflow,
            researcher_workflow,
            deliberation_workflow,
            conclusion_workflow,
            playwright_workflow,
        ):
            with self.subTest(module=module.__name__):
                for name in (
                    "LAYER_ID",
                    "MANAGER_ID",
                    "AGENT_IDS",
                    "AGENT_ORDER",
                    "DISPLAY_NAMES",
                ):
                    self.assertTrue(hasattr(module, name), name)

    def test_cli_rejects_two_operations_in_one_command(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parse_args(["--doctor", "--demo"])

    def test_default_summary_hides_large_internal_payloads(self) -> None:
        summary = format_state_summary(
            "conclusion",
            {
                "workflow_id": "workflow-1",
                "status": "WAITING_HUMAN_SELECTION",
                "completed_agents": ["a", "b"],
                "message_history": [{"payload": "very large"}],
                "position_candidates": [
                    {"position_candidate_id": "position_a", "title": "Candidate A"}
                ],
            },
        )
        self.assertIn("position_a: Candidate A", summary)
        self.assertNotIn("message_history", summary)
        self.assertNotIn("very large", summary)


if __name__ == "__main__":
    unittest.main()
