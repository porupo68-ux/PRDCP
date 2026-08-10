LAYER_ID = "playwright"
MANAGER_ID = "playwright.manager"

NARRATIVE_ARCHITECT_ID = "playwright.narrative_architect"
SCRIPTWRITER_ID = "playwright.scriptwriter"
EVIDENCE_CITATION_EDITOR_ID = "playwright.evidence_citation_editor"
VISUAL_DIRECTOR_ID = "playwright.visual_director"

AGENT_ORDER = [
    NARRATIVE_ARCHITECT_ID,
    SCRIPTWRITER_ID,
    EVIDENCE_CITATION_EDITOR_ID,
    VISUAL_DIRECTOR_ID,
]
AGENT_IDS = AGENT_ORDER

DISPLAY_NAMES = {
    NARRATIVE_ARCHITECT_ID: "Narrative Architect",
    SCRIPTWRITER_ID: "Scriptwriter",
    EVIDENCE_CITATION_EDITOR_ID: "Evidence & Citation Editor",
    VISUAL_DIRECTOR_ID: "Visual Director",
}

REVISION_DEPENDENCIES = {
    NARRATIVE_ARCHITECT_ID: AGENT_ORDER + ["manager.final_gate"],
    SCRIPTWRITER_ID: AGENT_ORDER[1:] + ["manager.final_gate"],
    EVIDENCE_CITATION_EDITOR_ID: AGENT_ORDER[2:] + ["manager.final_gate"],
    VISUAL_DIRECTOR_ID: [VISUAL_DIRECTOR_ID, "manager.final_gate"],
}
