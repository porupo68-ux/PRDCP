import asyncio
import unittest

from discord_app.bot import (
    DISCORD_OPERATIONAL_ERROR_LIMIT,
    report_execution_error,
    summarize_operational_error,
)


class _FakeContext:
    def __init__(self):
        self.guild = object()
        self.sent = []

    async def send(self, message):
        self.sent.append(message)


class DiscordErrorHandlingTests(unittest.TestCase):
    def test_long_exception_is_reduced_to_single_bounded_operational_summary(self):
        summary = summarize_operational_error(
            ValueError("first line\n" + "x" * 5000 + "\nlast line")
        )

        self.assertLessEqual(len(summary), DISCORD_OPERATIONAL_ERROR_LIMIT)
        self.assertNotIn("\n", summary)
        self.assertTrue(summary.startswith("ValueError:"))

    def test_execution_failure_closes_running_status_as_error(self):
        ctx = _FakeContext()
        routed = []

        async def route_status(guild, workflow_id, layer, status):
            routed.append((guild, workflow_id, layer, status))

        try:
            raise ValueError("handoff validation failed " + "x" * 5000)
        except ValueError as error:
            with self.assertLogs("discord_app.bot", level="ERROR") as captured:
                asyncio.run(
                    report_execution_error(
                        ctx,
                        "Deliberation",
                        "workflow_cycle041",
                        error,
                        route_status,
                    )
                )

        self.assertEqual(len(ctx.sent), 1)
        self.assertLess(len(ctx.sent[0]), 800)
        full_log = "\n".join(captured.output)
        self.assertIn("handoff validation failed", full_log)
        self.assertGreater(len(full_log), 5000)
        self.assertEqual(
            routed,
            [(ctx.guild, "workflow_cycle041", "Deliberation", "ERROR")],
        )


if __name__ == "__main__":
    unittest.main()
