from common.models.pmp import MessageType
from conclusion.agents.base import ConclusionAgent
from conclusion.schemas.decision_integration import DecisionIntegrationResult, DecisionIntegrationTask


class DecisionIntegrator(ConclusionAgent):
    agent_id = "conclusion.decision_integrator"
    input_schema = DecisionIntegrationTask
    output_schema = DecisionIntegrationResult
    output_message_type = MessageType.DECISION_INTEGRATION_RESULT
    accepted_message_types = {MessageType.DECISION_INTEGRATION_ASSIGNMENT.value}
