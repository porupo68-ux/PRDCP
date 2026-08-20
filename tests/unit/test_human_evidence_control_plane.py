import unittest

from cli_app.arguments import parse_args
from cli_app.output import next_action_for
from discord_app.channel_router import COMMAND_CHANNEL_RULES


class HumanEvidenceControlPlaneTests(unittest.TestCase):
    def test_cli_operations_are_mutually_exclusive_and_keep_reason(self):
        parsed = parse_args(
            [
                "--researcher-accept-limitations",
                "workflow_test",
                "--reason",
                "Deadline and disclosed risk were reviewed",
            ]
        )
        self.assertEqual(parsed.researcher_accept_limitations, "workflow_test")
        self.assertEqual(
            parsed.reason, "Deadline and disclosed risk were reviewed"
        )
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--researcher-accept",
                    "workflow_test",
                    "--researcher-revise",
                    "workflow_test",
                ]
            )

    def test_waiting_status_points_to_human_decision_not_recovery(self):
        action = next_action_for(
            "researcher",
            {
                "workflow_id": "workflow_test",
                "status": "WAITING_HUMAN_EVIDENCE_REVIEW",
            },
        )
        self.assertIn("--researcher-evidence", action)
        self.assertIn("--researcher-accept-limitations", action)
        self.assertNotIn("--researcher-recover", action)

    def test_legacy_blocked_review_points_to_zero_call_gate_recovery(self):
        action = next_action_for(
            "researcher",
            {
                "workflow_id": "workflow_test",
                "status": "BLOCKED",
                "review_result": {"status": "revision_required"},
                "human_evidence_decision": None,
                "error": {"message": "legacy automatic revision stop"},
            },
        )
        self.assertIn("--researcher-recover workflow_test", action)
        self.assertIn("0-call", action)

    def test_duplicate_tracking_hard_finding_points_to_integrity_repair(self):
        action = next_action_for(
            "researcher",
            {
                "workflow_id": "workflow_test",
                "status": "BLOCKED",
                "review_result": {
                    "status": "revision_required",
                    "findings": [
                        {
                            "finding_type": "HARD_INTEGRITY_FAILURE",
                            "issue": "same-document duplicate tracking is missing",
                            "required_action": "populate merged_evidence_ids",
                        }
                    ],
                },
                "human_evidence_decision": None,
            },
        )
        self.assertEqual(
            action,
            "py main.py --researcher-integrity-repair workflow_test",
        )

    def test_all_discord_human_gate_commands_are_routed_to_researcher(self):
        for command in (
            "researcher_evidence",
            "researcher_accept",
            "researcher_accept_limitations",
            "researcher_revise",
            "researcher_recover",
        ):
            with self.subTest(command=command):
                self.assertEqual(COMMAND_CHANNEL_RULES[command], "researcher")


if __name__ == "__main__":
    unittest.main()
