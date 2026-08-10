from deliberation.agents.base import DeliberationAgent
from deliberation.schemas.analysis_task import DeliberationAnalysisTask
from deliberation.schemas.stakeholder_response_analysis import StakeholderResponseAnalysisResult


class StakeholderResponseAnalyst(DeliberationAgent):
    agent_id = "deliberation.stakeholder_response_analyst"
    input_schema = DeliberationAnalysisTask
    output_schema = StakeholderResponseAnalysisResult
