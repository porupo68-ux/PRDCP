from __future__ import annotations

from common.agents import StructuredAgent


class ConclusionAgent(StructuredAgent):
    """Conclusion adapter for the shared structured-agent pipeline."""

    prompt_layer = "conclusion"
    manager_agent_id = "conclusion.manager"
