from common.models.pmp import MessageType
from conclusion.agents.base import ConclusionAgent
from conclusion.schemas.review import ConclusionQualityReviewInput, ConclusionQualityReviewOutput


class ConclusionQualityReviewer(ConclusionAgent):
    agent_id = "conclusion.quality_reviewer"
    input_schema = ConclusionQualityReviewInput
    output_schema = ConclusionQualityReviewOutput
    output_message_type = MessageType.CONCLUSION_QUALITY_REVIEW_RESULT
    accepted_message_types = {MessageType.CONCLUSION_QUALITY_REVIEW_ASSIGNMENT.value}
