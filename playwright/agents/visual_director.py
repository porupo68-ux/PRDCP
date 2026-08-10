from playwright.agents.base import PlaywrightAgent
from playwright.schemas.visual_plan import VisualDirectionTask, VisualPlan


class VisualDirector(PlaywrightAgent):
    agent_id = "playwright.visual_director"
    input_schema = VisualDirectionTask
    output_schema = VisualPlan

