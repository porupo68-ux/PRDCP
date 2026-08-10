from common.models.pmp import MessageType
from researcher.agents.base import ResearcherAgent
from researcher.schemas.review import ResearchQualityReviewInput, ResearchQualityReviewOutput


class QualityReviewer(ResearcherAgent):
    agent_id = "researcher.quality_reviewer"
    input_schema = ResearchQualityReviewInput
    output_schema = ResearchQualityReviewOutput
    output_message_type = MessageType.REVIEW
    accepted_message_types = {MessageType.TASK.value, MessageType.INFO.value}
