LAYER_ID = "deliberation"
MANAGER_ID = "deliberation.manager"

PRIMARY_ANALYST_IDS = [
    "deliberation.argument_analyst",
    "deliberation.causal_structural_analyst",
    "deliberation.stakeholder_response_analyst",
]
COUNTERARGUMENT_ANALYST_ID = "deliberation.counterargument_analyst"
QUALITY_REVIEWER_ID = "deliberation.quality_reviewer"
AGENT_ORDER = PRIMARY_ANALYST_IDS + [COUNTERARGUMENT_ANALYST_ID, QUALITY_REVIEWER_ID]
AGENT_IDS = AGENT_ORDER

DISPLAY_NAMES = {
    "deliberation.argument_analyst": "Argument Analyst",
    "deliberation.causal_structural_analyst": "Causal & Structural Analyst",
    "deliberation.stakeholder_response_analyst": "Stakeholder & Response Analyst",
    "deliberation.counterargument_analyst": "Counterargument Analyst",
    "deliberation.quality_reviewer": "Deliberation Quality Reviewer",
}
