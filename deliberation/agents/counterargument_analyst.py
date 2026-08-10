from deliberation.agents.base import DeliberationAgent
from deliberation.schemas.analysis_task import CounterargumentTask
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult


class CounterargumentAnalyst(DeliberationAgent):
    agent_id = "deliberation.counterargument_analyst"
    input_schema = CounterargumentTask
    output_schema = CounterargumentAnalysisResult
