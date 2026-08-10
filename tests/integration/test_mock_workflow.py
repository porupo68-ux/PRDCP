import asyncio
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from discord_app.commands import load_producer_status, run_producer
from discord_app.message_formatter import format_result, format_status
from providers.mock_provider import MockModelProvider
from runtime import build_manager


class MockWorkflowIntegrationTests(unittest.TestCase):
    def test_command_service_to_researcher_inbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(
                provider="mock",
                discord_bot_token=None,
                openrouter_api_key=None,
                openrouter_base_url="https://openrouter.ai/api/v1",
                data_dir=Path(temporary),
                log_level="INFO",
                models={},
            )
            manager = build_manager(settings, provider=MockModelProvider())
            updates = []

            async def run():
                return await run_producer(
                    manager,
                    topic="生成AIは人間の仕事を奪うのか",
                    progress_callback=updates.append,
                )

            state = asyncio.run(run())
            restored = load_producer_status(manager, state.workflow_id)
            self.assertEqual(restored.status, "COMPLETED")
            self.assertIn("Status: COMPLETED", format_status(restored))
            self.assertIn('"research_plan"', format_result(restored))
            self.assertEqual(len(updates), 6)
            self.assertTrue((Path(temporary) / "outbox" / "researcher" / f"{state.workflow_id}.json").exists())

    def test_user_topic_is_preserved_in_generated_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(
                provider="mock",
                discord_bot_token=None,
                openrouter_api_key=None,
                openrouter_base_url="https://openrouter.ai/api/v1",
                data_dir=Path(temporary),
                log_level="INFO",
                models={},
            )
            manager = build_manager(settings, provider=MockModelProvider())
            state = asyncio.run(run_producer(manager, topic="リモートワークは生産性を高めるのか"))
            self.assertEqual(state.status, "COMPLETED")
            self.assertEqual(state.research_plan["topic"], "リモートワークは生産性を高めるのか")
            self.assertIn("リモートワーク", state.research_plan["research_questions"][0]["question"])


if __name__ == "__main__":
    unittest.main()
