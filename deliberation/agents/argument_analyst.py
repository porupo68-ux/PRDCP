from deliberation.agents.base import DeliberationAgent
from deliberation.schemas.analysis_task import DeliberationAnalysisTask
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult


class ArgumentAnalyst(DeliberationAgent):
    agent_id = "deliberation.argument_analyst"
    input_schema = DeliberationAnalysisTask
    output_schema = ArgumentAnalysisResult
