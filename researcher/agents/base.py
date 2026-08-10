from __future__ import annotations

from pydantic import BaseModel

from common.agents import StructuredAgent
from common.models.pmp import MessageType, PMPMessage
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.research_task import ResearchTask


class ResearcherAgent(StructuredAgent):
    """Researcher adapter, including the research-revision message mapping."""

    input_schema: type[BaseModel] = ResearchTask
    output_schema: type[BaseModel] = ResearchResult
    prompt_layer = "researcher"
    manager_agent_id = "researcher.manager"
    result_objective_suffix = "evidence collection result"
    accepted_message_types = {
        MessageType.TASK.value,
        MessageType.INFO.value,
        MessageType.RESEARCH_REVISION_REQUEST.value,
    }

    def resolve_result_message_type(self, request: PMPMessage) -> MessageType:
        if request.message_type == MessageType.RESEARCH_REVISION_REQUEST.value:
            return MessageType.RESEARCH_REVISION_RESULT
        return self.output_message_type
