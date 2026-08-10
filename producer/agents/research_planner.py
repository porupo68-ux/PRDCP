from producer.agents.base import ProducerAgent
from producer.schemas.research_plan import ResearchPlanInput, ResearchPlanOutput


class ResearchPlanner(ProducerAgent):
    agent_id = "producer.research_planner"
    input_schema = ResearchPlanInput
    output_schema = ResearchPlanOutput

