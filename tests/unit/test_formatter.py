import unittest

from discord_app.message_formatter import format_researcher_sources, split_message


class MessageFormatterTests(unittest.TestCase):
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
