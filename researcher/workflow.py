from researcher.schemas.research_task import RESEARCH_TARGET_MAP


LAYER_ID = "researcher"
MANAGER_ID = "researcher.manager"
SPECIALIST_AGENT_IDS = list(RESEARCH_TARGET_MAP.values())
QUALITY_REVIEWER_ID = "researcher.quality_reviewer"
AGENT_ORDER = SPECIALIST_AGENT_IDS + [QUALITY_REVIEWER_ID]
AGENT_IDS = AGENT_ORDER

DISPLAY_NAMES = {
    "researcher.expert_researcher": "Expert Researcher",
    "researcher.academic_researcher": "Academic Researcher",
    "researcher.government_researcher": "Government Researcher",
    "researcher.news_researcher": "News Researcher",
    "researcher.public_opinion_researcher": "Public Opinion Researcher",
    "researcher.politician_researcher": "Politician Researcher",
    "researcher.industry_researcher": "Industry Researcher",
    "researcher.quality_reviewer": "Quality Reviewer",
}
