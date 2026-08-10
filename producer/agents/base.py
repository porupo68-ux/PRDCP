from __future__ import annotations

from common.agents import StructuredAgent


class ProducerAgent(StructuredAgent):
    """Producer adapter for the shared structured-agent pipeline."""

    prompt_layer = "producer"
    manager_agent_id = "producer.manager"
    result_objective_suffix = "execution result"
    error_next_stage = "abort"
    use_request_previous_stage = True
