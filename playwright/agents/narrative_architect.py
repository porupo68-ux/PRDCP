from playwright.agents.base import PlaywrightAgent
from playwright.schemas.narrative_blueprint import NarrativeBlueprint, NarrativeDesignTask


class NarrativeArchitect(PlaywrightAgent):
    agent_id = "playwright.narrative_architect"
    input_schema = NarrativeDesignTask
    output_schema = NarrativeBlueprint

