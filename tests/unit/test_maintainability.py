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
from scripts.verify import ACTIVE_PYTHON_TARGETS, PROJECT_ROOT, isolated_environment


class MaintainabilityTests(unittest.TestCase):
    def test_verification_compiles_only_active_python_surface(self) -> None:
        self.assertNotIn(".", ACTIVE_PYTHON_TARGETS)
        self.assertNotIn("archive", ACTIVE_PYTHON_TARGETS)
        self.assertNotIn("storage", ACTIVE_PYTHON_TARGETS)
        for target in ACTIVE_PYTHON_TARGETS:
            with self.subTest(target=target):
                self.assertTrue((PROJECT_ROOT / target).exists())

    def test_verification_forces_mock_isolated_storage(self) -> None:
        env = isolated_environment("isolated-test-data")
        self.assertEqual(env["PRDCP_PROVIDER"], "mock")
        self.assertEqual(env["PRDCP_RETRIEVAL_PROVIDER"], "mock")
        self.assertEqual(env["PRDCP_DATA_DIR"], "isolated-test-data")
        self.assertEqual(env["DISCORD_BOT_TOKEN"], "")

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

    def test_cli_accepts_deliberation_checkpoint_recovery(self) -> None:
        args = parse_args(["--deliberation-recover", "workflow-1"])
        self.assertEqual(args.deliberation_recover, "workflow-1")
        self.assertIsNone(args.deliberation_resume)

    def test_cli_accepts_producer_checkpoint_recovery(self) -> None:
        args = parse_args(["--producer-recover", "workflow-1"])
        self.assertEqual(args.producer_recover, "workflow-1")
        self.assertIsNone(args.producer_provider_retry)

    def test_cli_accepts_one_time_producer_provider_retry(self) -> None:
        args = parse_args(["--producer-provider-retry", "workflow-1"])
        self.assertEqual(args.producer_provider_retry, "workflow-1")
        self.assertIsNone(args.producer_recover)

    def test_cli_accepts_one_time_producer_output_repair(self) -> None:
        args = parse_args(["--producer-output-repair", "workflow-1"])
        self.assertEqual(args.producer_output_repair, "workflow-1")
        self.assertIsNone(args.producer_provider_retry)

    def test_cli_accepts_one_time_deliberation_provider_retry(self) -> None:
        args = parse_args(["--deliberation-provider-retry", "workflow-1"])
        self.assertEqual(args.deliberation_provider_retry, "workflow-1")
        self.assertIsNone(args.deliberation_recover)

    def test_cli_accepts_conclusion_checkpoint_recovery(self) -> None:
        args = parse_args(["--conclusion-recover", "workflow-1"])
        self.assertEqual(args.conclusion_recover, "workflow-1")
        self.assertIsNone(args.conclusion_resume)

    def test_cli_accepts_one_time_conclusion_provider_retry(self) -> None:
        args = parse_args(["--conclusion-provider-retry", "workflow-1"])
        self.assertEqual(args.conclusion_provider_retry, "workflow-1")

    def test_cli_accepts_playwright_checkpoint_recovery(self) -> None:
        args = parse_args(["--playwright-recover", "workflow-1"])
        self.assertEqual(args.playwright_recover, "workflow-1")
        self.assertIsNone(args.playwright_resume)

    def test_cli_accepts_one_time_playwright_provider_retry(self) -> None:
        args = parse_args(["--playwright-provider-retry", "workflow-1"])
        self.assertEqual(args.playwright_provider_retry, "workflow-1")
        self.assertIsNone(args.playwright_recover)

    def test_cli_accepts_one_explicit_playwright_revision_cycle(self) -> None:
        args = parse_args(["--playwright-revise", "workflow-1"])
        self.assertEqual(args.playwright_revise, "workflow-1")
        self.assertIsNone(args.playwright_recover)

    def test_cli_accepts_one_time_playwright_capability_repair_model(self) -> None:
        args = parse_args(
            [
                "--playwright-capability-repair",
                "workflow-1",
                "openai/gpt-5-mini",
            ]
        )
        self.assertEqual(
            args.playwright_capability_repair,
            ["workflow-1", "openai/gpt-5-mini"],
        )
        self.assertIsNone(args.playwright_recover)

    def test_cli_accepts_one_explicit_conclusion_revision_cycle(self) -> None:
        args = parse_args(["--conclusion-revise", "workflow-1"])
        self.assertEqual(args.conclusion_revise, "workflow-1")
        self.assertIsNone(args.conclusion_resume)

    def test_cli_accepts_one_time_conclusion_contract_repair_model(self) -> None:
        args = parse_args(
            [
                "--conclusion-contract-repair",
                "workflow-1",
                "openai/gpt-5-mini",
            ]
        )
        self.assertEqual(
            args.conclusion_contract_repair,
            ["workflow-1", "openai/gpt-5-mini"],
        )
        self.assertIsNone(args.conclusion_recover)

    def test_cli_accepts_researcher_external_revision_resume(self) -> None:
        args = parse_args(["--researcher-resume", "workflow-1"])
        self.assertEqual(args.researcher_resume, "workflow-1")
        self.assertIsNone(args.researcher)

    def test_cli_accepts_one_time_researcher_provider_retry(self) -> None:
        args = parse_args(["--researcher-provider-retry", "workflow-1"])
        self.assertEqual(args.researcher_provider_retry, "workflow-1")
        self.assertIsNone(args.researcher_resume)

    def test_cli_accepts_one_time_researcher_retrieval_reconstruction(self) -> None:
        args = parse_args(["--researcher-retrieval-reconstruct", "workflow-1"])
        self.assertEqual(args.researcher_retrieval_reconstruct, "workflow-1")
        self.assertIsNone(args.researcher_runtime_model_repair)

    def test_cli_accepts_one_time_researcher_runtime_output_repair(self) -> None:
        args = parse_args(["--researcher-runtime-output-repair", "workflow-1"])
        self.assertEqual(args.researcher_runtime_output_repair, "workflow-1")
        self.assertIsNone(args.researcher_retrieval_reconstruct)

    def test_cli_accepts_one_time_researcher_runtime_adapter_repair(self) -> None:
        args = parse_args(["--researcher-runtime-adapter-repair", "workflow-1"])
        self.assertEqual(args.researcher_runtime_adapter_repair, "workflow-1")
        self.assertIsNone(args.researcher_runtime_output_repair)

    def test_cli_accepts_one_time_researcher_runtime_identity_repair(self) -> None:
        args = parse_args(["--researcher-runtime-identity-repair", "workflow-1"])
        self.assertEqual(args.researcher_runtime_identity_repair, "workflow-1")
        self.assertIsNone(args.researcher_runtime_adapter_repair)

    def test_cli_accepts_one_time_researcher_runtime_provenance_repair(self) -> None:
        args = parse_args(["--researcher-runtime-provenance-repair", "workflow-1"])
        self.assertEqual(args.researcher_runtime_provenance_repair, "workflow-1")
        self.assertIsNone(args.researcher_runtime_identity_repair)

    def test_cli_accepts_one_explicit_researcher_revision_cycle(self) -> None:
        args = parse_args(["--researcher-revise", "workflow-1"])
        self.assertEqual(args.researcher_revise, "workflow-1")
        self.assertIsNone(args.researcher_resume)

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

    def test_blocked_conclusion_revision_summary_shows_explicit_safe_action(self) -> None:
        summary = format_state_summary(
            "conclusion",
            {
                "workflow_id": "workflow-1",
                "status": "BLOCKED",
                "review_result": {
                    "status": "revision_required",
                    "revision_scope": "targeted",
                    "revision_targets": ["conclusion.position_generator"],
                },
            },
        )

        self.assertIn(
            "py main.py --conclusion-revise workflow-1 --safe-mode",
            summary,
        )


if __name__ == "__main__":
    unittest.main()
