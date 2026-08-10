from common.models.pmp import MessageType
from conclusion.agents.base import ConclusionAgent
from conclusion.schemas.decision_evaluation import DecisionEvaluationResult, DecisionEvaluationTask


class DecisionEvaluator(ConclusionAgent):
    agent_id = "conclusion.decision_evaluator"
    input_schema = DecisionEvaluationTask
    output_schema = DecisionEvaluationResult
    output_message_type = MessageType.DECISION_EVALUATION_RESULT
    accepted_message_types = {MessageType.DECISION_EVALUATION_ASSIGNMENT.value}
