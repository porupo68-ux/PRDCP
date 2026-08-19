import unittest

from discord_app.message_formatter import format_researcher_sources, format_status, split_message
from producer.state import ProducerWorkflowState


class MessageFormatterTests(unittest.TestCase):
    def test_failed_producer_does_not_list_pending_agents_as_completed(self):
        state = ProducerWorkflowState(
            workflow_id="workflow-failed",
            initial_request={"topic": "test"},
            status="FAILED",
            completed_agents=[],
            error={"message": "provider failure"},
        )
        text = format_status(state)
        completed_section = text.split("Completed:\n", 1)[1].split("\n\nCurrent:", 1)[0]
        self.assertEqual(completed_section, "(none)")
        self.assertIn("Pending:", text)
        self.assertIn("· Topic Scout", text)
        self.assertNotIn("✓ Topic Scout", text)

    def test_long_json_code_block_is_split_into_valid_code_blocks(self):
        text = "```json\n" + ("{\"value\": 1}\n" * 300) + "```"
        chunks = split_message(text, limit=300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.startswith("```json\n") for chunk in chunks))
        self.assertTrue(all(chunk.endswith("\n```") for chunk in chunks))
        self.assertTrue(all(len(chunk) <= 300 for chunk in chunks))

    def test_researcher_sources_are_formatted_for_audit_channel(self):
        text = format_researcher_sources(
            {
                "workflow_id": "workflow-1",
                "sources": [
                    {
                        "source_id": "SRC-001",
                        "source_type": "ACADEMIC",
                        "title": "Example study",
                        "url": "https://example.com/study",
                        "source_name": "Academic Researcher",
                        "evidence_id": "EV-001",
                    }
                ],
            }
        )
        self.assertIn("Sources\nWorkflow: workflow-1", text)
        self.assertIn("[Academic]", text)
        self.assertIn("SRC-001", text)
        self.assertIn("Evidence IDs: EV-001", text)


if __name__ == "__main__":
    unittest.main()
