import asyncio
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from discord_app.commands import (
    load_playwright_result,
    run_conclusion,
    run_deliberation,
    run_playwright,
    run_producer,
    run_researcher,
)
from providers.mock_provider import MockModelProvider
from runtime import build_all_managers, build_playwright_manager


class PlaywrightIntegrationTests(unittest.TestCase):
    @staticmethod
    def settings(data_dir: Path) -> Settings:
        return Settings(
            provider="mock",
            discord_bot_token=None,
            openrouter_api_key=None,
            openrouter_base_url="https://openrouter.ai/api/v1",
            data_dir=data_dir,
            log_level="INFO",
            models={},
        )

    def test_producer_to_final_script_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            settings = self.settings(data_dir)
            provider = MockModelProvider()
            producer, researcher, deliberation, conclusion, playwright = build_all_managers(
                settings, provider=provider
            )
            producer_state = asyncio.run(run_producer(producer, topic="生成AIは人間の仕事を奪うのか"))
            asyncio.run(run_researcher(researcher, workflow_id=producer_state.workflow_id))
            asyncio.run(run_deliberation(deliberation, workflow_id=producer_state.workflow_id))
            waiting = asyncio.run(run_conclusion(conclusion, workflow_id=producer_state.workflow_id))
            selected_id = waiting.position_candidates[0]["position_candidate_id"]
            completed_conclusion = conclusion.select(waiting.workflow_id, [selected_id])
            completed_playwright = asyncio.run(
                run_playwright(playwright, workflow_id=waiting.workflow_id)
            )
            package = load_playwright_result(playwright, waiting.workflow_id)
            self.assertEqual(completed_conclusion.status, "COMPLETED")
            self.assertEqual(completed_playwright.status, "COMPLETED")
            self.assertEqual(package.workflow_id, waiting.workflow_id)
            self.assertGreaterEqual(len(package.script.sections), 4)
            self.assertTrue(package.citation_manifest.mappings)
            self.assertTrue(package.visual_plan.visual_cues)

    def test_restart_can_load_conclusion_outbox_and_run_playwright(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            settings = self.settings(data_dir)
            provider = MockModelProvider()
            producer, researcher, deliberation, conclusion, _playwright = build_all_managers(
                settings, provider=provider
            )
            producer_state = asyncio.run(run_producer(producer, topic="再起動テスト"))
            asyncio.run(run_researcher(researcher, workflow_id=producer_state.workflow_id))
            asyncio.run(run_deliberation(deliberation, workflow_id=producer_state.workflow_id))
            waiting = asyncio.run(run_conclusion(conclusion, workflow_id=producer_state.workflow_id))
            conclusion.select(waiting.workflow_id, [waiting.position_candidates[0]["position_candidate_id"]])

            restarted = build_playwright_manager(settings, provider=MockModelProvider())
            state = asyncio.run(restarted.start(waiting.workflow_id))
            self.assertEqual(state.status, "COMPLETED")
            self.assertTrue(restarted.repository.load_final_package(waiting.workflow_id))


if __name__ == "__main__":
    unittest.main()
