from common.models.pmp import MessageType
from conclusion.agents.base import ConclusionAgent
from conclusion.schemas.position_candidate import PositionGenerationResult, PositionGenerationTask


class PositionGenerator(ConclusionAgent):
    agent_id = "conclusion.position_generator"
    input_schema = PositionGenerationTask
    output_schema = PositionGenerationResult
    output_message_type = MessageType.POSITION_GENERATION_RESULT
    accepted_message_types = {MessageType.POSITION_GENERATION_ASSIGNMENT.value}
