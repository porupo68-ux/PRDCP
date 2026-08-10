from producer.agents.base import ProducerAgent
from producer.schemas.topic_scout import TopicScoutInput, TopicScoutOutput


class TopicScout(ProducerAgent):
    agent_id = "producer.topic_scout"
    input_schema = TopicScoutInput
    output_schema = TopicScoutOutput

