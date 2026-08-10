from playwright.agents.base import PlaywrightAgent
from playwright.schemas.citation_manifest import CitationEditingResult, CitationEditingTask


class EvidenceCitationEditor(PlaywrightAgent):
    agent_id = "playwright.evidence_citation_editor"
    input_schema = CitationEditingTask
    output_schema = CitationEditingResult

