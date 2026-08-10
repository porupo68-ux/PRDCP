import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from discord_app.commands import run_deliberation, run_producer, run_researcher
from providers.mock_provider import MockModelProvider
from runtime import build_managers


class DeliberationIntegrationTests(unittest.TestCase):
    def test_producer_researcher_deliberation_to_conclusion(self):
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
            provider = MockModelProvider()
            producer, researcher, deliberation = build_managers(settings, provider=provider)
            producer_state = asyncio.run(
                run_producer(
                    producer,
                    topic="生成AIは人間の仕事を奪うのか",
                )
            )
            research_state = asyncio.run(
                run_researcher(researcher, workflow_id=producer_state.workflow_id)
            )
            deliberation_state = asyncio.run(
                run_deliberation(deliberation, workflow_id=producer_state.workflow_id)
            )
            self.assertEqual(producer_state.status, "COMPLETED")
            self.assertEqual(research_state.status, "COMPLETED")
            self.assertEqual(deliberation_state.status, "COMPLETED")
            outbox = (
                Path(temporary)
                / "outbox"
                / "conclusion"
                / f"{producer_state.workflow_id}.json"
            )
            message = json.loads(outbox.read_text(encoding="utf-8"))
            self.assertEqual(message["sender_agent_id"], "deliberation.manager")
            self.assertEqual(message["receiver_agent_id"], "conclusion.manager")
            self.assertEqual(message["message_type"], "deliberation_result")
            self.assertTrue(message["payload"]["source_traceability"])
            self.assertTrue(message["payload"]["analysis_traceability"])


if __name__ == "__main__":
    unittest.main()
