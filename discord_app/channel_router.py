from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from discord_app.message_formatter import split_message


CHANNEL_MAP = {
    "producer": "producer",
    "researcher": "researcher",
    "deliberation": "deliberation",
    "conclusion": "conclusion",
    "playwright": "playwright",
    "sources": "sources",
    "deliveries": "deliveries",
    "status": "workflow-status",
}


COMMAND_CHANNEL_RULES = {
    "producer": "producer",
    "producer_topic": "producer",
    "producer_status": "producer",
    "producer_revise": "producer",
    "researcher": "researcher",
    "researcher_status": "researcher",
    "researcher_result": "researcher",
    "researcher_evidence": "researcher",
    "researcher_accept": "researcher",
    "researcher_accept_limitations": "researcher",
    "researcher_revise": "researcher",
    "researcher_revision_execute": "researcher",
    "deliberation_revise": "deliberation",
    "researcher_recover": "researcher",
    "deliberation": "deliberation",
    "deliberation_status": "deliberation",
    "deliberation_result": "deliberation",
    "deliberation_resume": "deliberation",
    "conclusion": "conclusion",
    "conclusion_status": "conclusion",
    "conclusion_options": "conclusion",
    "conclusion_select": "conclusion",
    "conclusion_integrate": "conclusion",
    "conclusion_result": "conclusion",
    "conclusion_resume": "conclusion",
    "conclusion_revise": "conclusion",
    "playwright": "playwright",
    "playwright_status": "playwright",
    "playwright_script": "playwright",
    "playwright_citations": "playwright",
    "playwright_visuals": "playwright",
    "playwright_result": "playwright",
    "playwright_resume": "playwright",
    "playwright_revise": "playwright",
}


class ChannelRoutingError(RuntimeError):
    """Raised when a Discord routing invariant cannot be satisfied."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        target: str | None = None,
        channel_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.target = target
        self.channel_name = channel_name


class ChannelRouter:
    """Resolve PRDCP destinations and enforce their Discord command boundaries."""

    def __init__(
        self,
        channel_map: Mapping[str, str] | None = None,
        *,
        command_channel_rules: Mapping[str, str] | None = None,
    ) -> None:
        selected_channels = CHANNEL_MAP if channel_map is None else channel_map
        selected_rules = (
            COMMAND_CHANNEL_RULES if command_channel_rules is None else command_channel_rules
        )
        self.channel_map = dict(selected_channels)
        self.command_channel_rules = dict(selected_rules)

    def channel_name(self, target: str) -> str:
        """Return the configured Discord channel name for a router target."""
        try:
            return self.channel_map[target]
        except (KeyError, TypeError) as exc:
            raise ChannelRoutingError(
                f"Unknown channel target: {target!r}",
                code="UNKNOWN_TARGET",
                target=target,
            ) from exc

    def get_channel(self, guild: Any, target: str) -> Any:
        """Resolve a target to one guild text channel without using a fallback."""
        channel_name = self.channel_name(target)
        if guild is None:
            raise ChannelRoutingError(
                "A Discord guild context is required for channel routing",
                code="NO_GUILD_CONTEXT",
                target=target,
                channel_name=channel_name,
            )

        channel = next(
            (
                item
                for item in getattr(guild, "text_channels", ())
                if getattr(item, "name", None) == channel_name
            ),
            None,
        )
        if channel is None:
            raise ChannelRoutingError(
                f"Required Discord channel not found: #{channel_name}",
                code="CHANNEL_NOT_FOUND",
                target=target,
                channel_name=channel_name,
            )
        return channel

    async def send(self, guild: Any, target: str, text: str) -> None:
        """Send one message to a resolved PRDCP channel."""
        channel = self.get_channel(guild, target)
        try:
            await channel.send(text)
        except Exception as exc:
            raise ChannelRoutingError(
                f"Failed to send a message to #{channel.name}",
                code="SEND_FAILED",
                target=target,
                channel_name=channel.name,
            ) from exc

    async def send_chunks(self, guild: Any, target: str, text: str) -> None:
        """Send text through the project's shared Discord 2000-character splitter."""
        channel = self.get_channel(guild, target)
        for chunk in split_message(text):
            try:
                await channel.send(chunk)
            except Exception as exc:
                raise ChannelRoutingError(
                    f"Failed to send a message to #{channel.name}",
                    code="SEND_FAILED",
                    target=target,
                    channel_name=channel.name,
                ) from exc

    def is_allowed_channel(self, ctx: Any, target: str) -> bool:
        """Return whether a command context belongs to the target channel."""
        required_channel = self.get_channel(getattr(ctx, "guild", None), target)
        current_channel = getattr(ctx, "channel", None)
        required_id = getattr(required_channel, "id", None)
        current_id = getattr(current_channel, "id", None)
        if required_id is not None and current_id is not None:
            return required_id == current_id
        return current_channel is required_channel

    async def require_channel(self, ctx: Any, target: str) -> bool:
        """Reject a command outside its PRDCP channel and explain where to run it."""
        if self.is_allowed_channel(ctx, target):
            return True
        channel_name = self.channel_name(target)
        await ctx.send(f"このコマンドは #{channel_name} で実行してください。")
        return False

    def target_for_command(self, command_name: str) -> str | None:
        """Return the routing target for a command, or None for an unrestricted command."""
        return self.command_channel_rules.get(command_name)

    async def require_command_channel(self, ctx: Any, command_name: str) -> bool:
        """Apply COMMAND_CHANNEL_RULES to one Discord command context."""
        target = self.target_for_command(command_name)
        if target is None:
            return True
        return await self.require_channel(ctx, target)


__all__ = [
    "CHANNEL_MAP",
    "COMMAND_CHANNEL_RULES",
    "ChannelRouter",
    "ChannelRoutingError",
]
