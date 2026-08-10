from producer.agents.base import ProducerAgent
from producer.schemas.topic_selector import TopicSelectorInput, TopicSelectorOutput


class TopicSelector(ProducerAgent):
    agent_id = "producer.topic_selector"
    input_schema = TopicSelectorInput
    output_schema = TopicSelectorOutput

