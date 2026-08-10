from __future__ import annotations

from datetime import datetime, timezone

from common.models.pmp import MessageStatus, MessageType, PMPMessage, PMPMetadata
from producer.schemas.research_plan import ResearchPlan


def make_plan() -> ResearchPlan:
    return ResearchPlan.model_validate(
        {
            "research_plan_id": "plan_test",
            "topic_id": "topic_test",
            "topic": "生成AIと雇用",
            "general_opinion_id": "opinion_test",
            "general_opinion": "生成AIは多くの仕事を奪う",
            "research_questions": [
                {
                    "research_question_id": "rq_employment",
                    "question": "雇用者数と仕事内容はどう変化したか",
                    "research_targets": ["ACADEMIC", "GOVERNMENT", "INDUSTRY"],
                },
                {
                    "research_question_id": "rq_views",
                    "question": "関係者は変化をどう認識しているか",
                    "research_targets": ["EXPERT", "NEWS", "PUBLIC_OPINION", "POLITICIAN"],
                },
            ],
            "scope": ["日本", "2022年以降"],
            "constraints": ["一次情報を優先", "結論を出さない"],
        }
    )


def make_handoff(plan: ResearchPlan | None = None) -> PMPMessage:
    plan = plan or make_plan()
    return PMPMessage.create(
        sender_agent_id="producer.manager",
        receiver_agent_id="researcher.manager",
        message_type=MessageType.RESEARCH_PLAN,
        objective="Begin Researcher workflow",
        payload=plan.model_dump(mode="json"),
        metadata=PMPMetadata(status=MessageStatus.COMPLETED),
    )


def valid_source(category: str = "ACADEMIC", **overrides) -> dict:
    metadata = {
        "EXPERT": {
            "expert_name": "Expert",
            "field": "AI",
            "affiliation": "Institute",
            "statement_context": "interview",
        },
        "ACADEMIC": {
            "doi": "10.0000/test",
            "peer_reviewed": True,
            "journal_name": "Journal",
            "study_type": "OBSERVATIONAL",
        },
        "GOVERNMENT": {
            "organization": "Ministry",
            "country": "Japan",
            "document_type": "STATISTICS",
        },
        "NEWS": {"media_name": "News", "article_type": "REPORTING"},
        "PUBLIC_OPINION": {
            "platform": "FORUM",
            "engagement_count": 1,
            "sample_size": None,
            "representativeness_warning": True,
        },
        "POLITICIAN": {
            "politician_name": "Person",
            "party": None,
            "position": None,
            "statement_type": "PARLIAMENT",
        },
        "INDUSTRY": {
            "organization_name": "Association",
            "organization_type": "INDUSTRY_ASSOCIATION",
            "industry": "technology",
        },
    }[category]
    data = {
        "source_id": "source_1",
        "evidence_id": "evidence_1",
        "research_question_ids": ["rq_employment"],
        "source_type": category,
        "title": "A traceable source",
        "source_name": "Source publisher",
        "url": "https://example.invalid/source",
        "author_or_organization": "Source publisher",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "summary": "A neutral evidence summary",
        "relevant_excerpt": None,
        "stance": "UNKNOWN",
        "reliability": "HIGH",
        "directness": "DIRECT",
        "primary_source": True,
        "geographic_scope": ["日本"],
        "time_scope": "2022年以降",
        "limitations": [],
        "source_specific_metadata": metadata,
    }
    data.update(overrides)
    return data
