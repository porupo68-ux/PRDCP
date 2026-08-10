from __future__ import annotations

from common.agents import StructuredAgent
from common.models.pmp import MessageType


class DeliberationAgent(StructuredAgent):
    """Deliberation adapter for the shared structured-agent pipeline."""

    prompt_layer = "deliberation"
    manager_agent_id = "deliberation.manager"
    output_message_type = MessageType.DELIBERATION_TASK_RESULT
    result_objective_suffix = "analysis result"
    accepted_message_types = {
        MessageType.DELIBERATION_TASK_ASSIGNMENT.value,
        MessageType.INFO.value,
    }
