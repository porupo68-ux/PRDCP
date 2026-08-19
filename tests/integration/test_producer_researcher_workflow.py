import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from config.settings import Settings
from discord_app.commands import (
    load_researcher_result,
    load_researcher_status,
    run_producer,
    run_researcher,
)
from discord_app.message_formatter import format_researcher_result, format_researcher_status
from providers.mock_provider import MockModelProvider
from researcher.schemas.human_evidence import HumanActorSource, HumanEvidenceDecisionType
from runtime import build_producer_researcher_managers


class ProducerResearcherIntegrationTests(unittest.TestCase):
    def test_producer_to_deliberation_contract(self):
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
            producer_manager, researcher_manager = build_producer_researcher_managers(
                settings,
                provider=provider,
            )
            producer_state = asyncio.run(
                run_producer(
                    producer_manager,
                    topic="生成AIは人間の仕事を奪うのか",
                )
            )
            research_state = asyncio.run(
                run_researcher(
                    researcher_manager,
                    workflow_id=producer_state.workflow_id,
                )
            )
            self.assertEqual(research_state.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
            research_state = researcher_manager.decide_human_evidence(
                producer_state.workflow_id,
                HumanEvidenceDecisionType.ACCEPT,
                reason="Explicit integration-test Human Evidence decision",
                actor_source=HumanActorSource.MOCK_FIXTURE,
            )
            self.assertEqual(producer_state.status, "COMPLETED")
            self.assertEqual(research_state.status, "COMPLETED")
            restored = load_researcher_status(
                researcher_manager,
                producer_state.workflow_id,
            )
            report = load_researcher_result(
                researcher_manager,
                producer_state.workflow_id,
            )
            self.assertIn("Status: COMPLETED", format_researcher_status(restored))
            self.assertIn('"research_report"', format_researcher_result(restored))
            self.assertEqual(report.research_plan_id, producer_state.research_plan["research_plan_id"])

            outbox = (
                Path(temporary)
                / "outbox"
                / "deliberation"
                / f"{producer_state.workflow_id}.json"
            )
            message = json.loads(outbox.read_text(encoding="utf-8"))
            payload = message["payload"]
            required = {
                "research_report_id",
                "research_plan_id",
                "topic",
                "general_opinion",
                "research_questions",
                "research_scope",
                "evidence_items",
                "source_metadata",
                "source_perspectives",
                "evidence_quality_assessments",
                "research_limitations",
                "unresolved_questions",
                "human_evidence_decision",
                "accepted_evidence_gaps",
                "human_evidence_integrity_repairs",
            }
            self.assertFalse(required - payload.keys())
            self.assertTrue(
                all(
                    item["evidence_id"] and item["source_id"]
                    for item in payload["evidence_items"]
                )
            )


if __name__ == "__main__":
    unittest.main()
