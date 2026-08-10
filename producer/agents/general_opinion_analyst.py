from producer.agents.base import ProducerAgent
from producer.schemas.general_opinion import GeneralOpinionInput, GeneralOpinionOutput


class GeneralOpinionAnalyst(ProducerAgent):
    agent_id = "producer.general_opinion_analyst"
    input_schema = GeneralOpinionInput
    output_schema = GeneralOpinionOutput

