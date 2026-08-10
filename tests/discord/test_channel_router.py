from __future__ import annotations

import asyncio
import unittest

from discord_app.channel_router import (
    CHANNEL_MAP,
    COMMAND_CHANNEL_RULES,
    ChannelRouter,
    ChannelRoutingError,
)


class FakeChannel:
    def __init__(self, channel_id: int, name: str, *, fail_send: bool = False) -> None:
        self.id = channel_id
        self.name = name
        self.fail_send = fail_send
        self.messages: list[str] = []

    async def send(self, text: str) -> None:
        if self.fail_send:
            raise RuntimeError("temporary Discord failure")
        self.messages.append(text)


class FakeGuild:
    def __init__(self, *channels: FakeChannel) -> None:
        self.text_channels = list(channels)


class FakeContext:
    def __init__(self, guild: FakeGuild | None, channel: FakeChannel) -> None:
        self.guild = guild
        self.channel = channel

    async def send(self, text: str) -> None:
        await self.channel.send(text)


class ChannelRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.channels = {
            name: FakeChannel(index, name)
            for index, name in enumerate(CHANNEL_MAP.values(), start=1)
        }
        self.guild = FakeGuild(*self.channels.values())
        self.router = ChannelRouter()

    def test_default_map_contains_all_eight_control_plane_channels(self) -> None:
        self.assertEqual(
            {
                "producer": "producer",
                "researcher": "researcher",
                "deliberation": "deliberation",
                "conclusion": "conclusion",
                "playwright": "playwright",
                "sources": "sources",
                "deliveries": "deliveries",
                "status": "workflow-status",
            },
            self.router.channel_map,
        )

    def test_get_channel_resolves_each_target(self) -> None:
        for target, channel_name in CHANNEL_MAP.items():
            with self.subTest(target=target):
                self.assertIs(
                    self.channels[channel_name],
                    self.router.get_channel(self.guild, target),
                )

    def test_unknown_target_is_an_explicit_error(self) -> None:
        with self.assertRaises(ChannelRoutingError) as raised:
            self.router.get_channel(self.guild, "general")
        self.assertEqual("UNKNOWN_TARGET", raised.exception.code)

    def test_missing_channel_does_not_fall_back(self) -> None:
        guild = FakeGuild(FakeChannel(999, "general"))
        with self.assertRaises(ChannelRoutingError) as raised:
            self.router.get_channel(guild, "sources")
        self.assertEqual("CHANNEL_NOT_FOUND", raised.exception.code)
        self.assertEqual("Required Discord channel not found: #sources", str(raised.exception))

    def test_guild_context_is_required(self) -> None:
        with self.assertRaises(ChannelRoutingError) as raised:
            self.router.get_channel(None, "producer")
        self.assertEqual("NO_GUILD_CONTEXT", raised.exception.code)

    def test_send_routes_only_to_the_requested_channel(self) -> None:
        asyncio.run(self.router.send(self.guild, "producer", "Topic Scout 完了"))
        self.assertEqual(["Topic Scout 完了"], self.channels["producer"].messages)
        self.assertFalse(
            any(
                channel.messages
                for name, channel in self.channels.items()
                if name != "producer"
            )
        )

    def test_send_chunks_uses_discord_message_limit(self) -> None:
        asyncio.run(self.router.send_chunks(self.guild, "researcher", "x" * 4500))
        messages = self.channels["researcher"].messages
        self.assertEqual(3, len(messages))
        self.assertTrue(all(len(message) <= 2000 for message in messages))

    def test_send_failure_is_not_swallowed(self) -> None:
        failing = FakeChannel(1, "producer", fail_send=True)
        with self.assertRaises(ChannelRoutingError) as raised:
            asyncio.run(self.router.send(FakeGuild(failing), "producer", "message"))
        self.assertEqual("SEND_FAILED", raised.exception.code)
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_wrong_command_channel_is_rejected_with_guidance(self) -> None:
        ctx = FakeContext(self.guild, self.channels["researcher"])
        allowed = asyncio.run(self.router.require_channel(ctx, "producer"))
        self.assertFalse(allowed)
        self.assertEqual(
            ["このコマンドは #producer で実行してください。"],
            self.channels["researcher"].messages,
        )

    def test_correct_command_channel_is_allowed(self) -> None:
        ctx = FakeContext(self.guild, self.channels["conclusion"])
        self.assertTrue(asyncio.run(self.router.require_channel(ctx, "conclusion")))
        self.assertEqual([], self.channels["conclusion"].messages)

    def test_every_layer_command_has_the_designed_target(self) -> None:
        for command_name, target in COMMAND_CHANNEL_RULES.items():
            with self.subTest(command_name=command_name):
                self.assertEqual(target, self.router.target_for_command(command_name))
        self.assertIsNone(self.router.target_for_command("help"))


if __name__ == "__main__":
    unittest.main()
