LAYER_ID = "producer"
MANAGER_ID = "producer.manager"

AGENT_ORDER = [
    "producer.topic_scout",
    "producer.topic_selector",
    "producer.general_opinion_analyst",
    "producer.research_planner",
    "producer.quality_reviewer",
]
AGENT_IDS = AGENT_ORDER

TRANSITIONS = {
    "producer.topic_scout": {"result": "producer.topic_selector", "error": "abort"},
    "producer.topic_selector": {"result": "producer.general_opinion_analyst", "error": "abort"},
    "producer.general_opinion_analyst": {"result": "producer.research_planner", "error": "abort"},
    "producer.research_planner": {"result": "producer.quality_reviewer", "error": "abort"},
    "producer.quality_reviewer": {
        "approved": "finish",
        "approved_with_conditions": "finish",
        "revision_required": "dynamic_revision_target",
        "blocked": "abort",
        "error": "abort",
    },
}

DISPLAY_NAMES = {
    "producer.topic_scout": "Topic Scout",
    "producer.topic_selector": "Topic Selector",
    "producer.general_opinion_analyst": "General Opinion Analyst",
    "producer.research_planner": "Research Planner",
    "producer.quality_reviewer": "Quality Reviewer",
}
