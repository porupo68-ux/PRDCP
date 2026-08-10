from common.models.pmp import MessageType
from deliberation.agents.base import DeliberationAgent
from deliberation.schemas.review import (
    DeliberationQualityReviewInput,
    DeliberationQualityReviewOutput,
)


class DeliberationQualityReviewer(DeliberationAgent):
    agent_id = "deliberation.quality_reviewer"
    input_schema = DeliberationQualityReviewInput
    output_schema = DeliberationQualityReviewOutput
    output_message_type = MessageType.DELIBERATION_QUALITY_REVIEW_RESULT
    accepted_message_types = {MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value}
