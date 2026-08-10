from deliberation.agents.base import DeliberationAgent
from deliberation.schemas.analysis_task import DeliberationAnalysisTask
from deliberation.schemas.causal_structural_analysis import CausalStructuralAnalysisResult


class CausalStructuralAnalyst(DeliberationAgent):
    agent_id = "deliberation.causal_structural_analyst"
    input_schema = DeliberationAnalysisTask
    output_schema = CausalStructuralAnalysisResult
