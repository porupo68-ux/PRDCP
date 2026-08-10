from common.models.pmp import MessageType
from producer.agents.base import ProducerAgent
from producer.schemas.review import QualityReviewInput, QualityReviewOutput


class QualityReviewer(ProducerAgent):
    agent_id = "producer.quality_reviewer"
    input_schema = QualityReviewInput
    output_schema = QualityReviewOutput
    output_message_type = MessageType.REVIEW

