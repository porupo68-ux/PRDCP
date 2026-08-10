import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from discord_app.commands import run_conclusion, run_deliberation, run_producer, run_researcher
from providers.mock_provider import MockModelProvider
from runtime import build_all_managers, build_conclusion_manager


class ConclusionIntegrationTests(unittest.TestCase):
    def settings(self, data_dir: Path) -> Settings:
        return Settings(
            provider="mock",
            discord_bot_token=None,
            openrouter_api_key=None,
            openrouter_base_url="https://openrouter.ai/api/v1",
            data_dir=data_dir,
            log_level="INFO",
            models={},
        )

    def test_four_layers_reach_human_selection_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider()
            producer, researcher, deliberation, conclusion, _playwright = build_all_managers(
                self.settings(data_dir), provider=provider
            )
            producer_state = asyncio.run(
                run_producer(producer, topic="生成AIは人間の仕事を奪うのか")
            )
            researcher_state = asyncio.run(
                run_researcher(researcher, workflow_id=producer_state.workflow_id)
            )
            deliberation_state = asyncio.run(
                run_deliberation(deliberation, workflow_id=producer_state.workflow_id)
            )
            conclusion_state = asyncio.run(
                run_conclusion(conclusion, workflow_id=producer_state.workflow_id)
            )
            self.assertEqual(producer_state.status, "COMPLETED")
            self.assertEqual(researcher_state.status, "COMPLETED")
            self.assertEqual(deliberation_state.status, "COMPLETED")
            self.assertEqual(conclusion_state.status, "WAITING_HUMAN_SELECTION")
            self.assertFalse(conclusion_state.playwright_sent)

    def test_restart_selection_creates_canonical_playwright_handoff(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            settings = self.settings(data_dir)
            provider = MockModelProvider()
            producer, researcher, deliberation, conclusion, _playwright = build_all_managers(
                settings, provider=provider
            )
            producer_state = asyncio.run(
                run_producer(producer, topic="生成AIは人間の仕事を奪うのか")
            )
            asyncio.run(run_researcher(researcher, workflow_id=producer_state.workflow_id))
            asyncio.run(run_deliberation(deliberation, workflow_id=producer_state.workflow_id))
            waiting = asyncio.run(run_conclusion(conclusion, workflow_id=producer_state.workflow_id))

            restarted = build_conclusion_manager(settings, provider=MockModelProvider())
            loaded = restarted.repository.load(waiting.workflow_id)
            selected_id = loaded.position_candidates[0]["position_candidate_id"]
            completed = restarted.select(loaded.workflow_id, [selected_id])
            self.assertEqual(completed.status, "COMPLETED")
            path = data_dir / "outbox" / "playwright" / f"{completed.workflow_id}.json"
            message = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(message["message_type"], "conclusion_handoff")
            self.assertEqual(message["payload"]["workflow_metadata"]["human_selection"]["selected_candidate_ids"], [selected_id])
            self.assertTrue(message["payload"]["supporting_claims"])
            self.assertTrue(message["payload"]["supporting_analysis"])
            self.assertTrue(message["payload"]["evidence_links"])


if __name__ == "__main__":
    unittest.main()
