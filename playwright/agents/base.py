from __future__ import annotations

from common.agents import StructuredAgent
from common.models.pmp import MessageType


class PlaywrightAgent(StructuredAgent):
    """Playwright adapter for the shared structured-agent pipeline."""

    prompt_layer = "playwright"
    manager_agent_id = "playwright.manager"
    accepted_message_types = {
        MessageType.TASK.value,
        MessageType.REVISION_REQUEST.value,
    }
