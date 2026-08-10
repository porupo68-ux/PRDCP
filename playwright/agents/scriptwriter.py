from playwright.agents.base import PlaywrightAgent
from playwright.schemas.script_draft import ScriptDraft, ScriptWritingTask


class Scriptwriter(PlaywrightAgent):
    agent_id = "playwright.scriptwriter"
    input_schema = ScriptWritingTask
    output_schema = ScriptDraft

