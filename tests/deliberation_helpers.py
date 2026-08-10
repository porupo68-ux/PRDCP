from __future__ import annotations

from pathlib import Path

from common.ids import new_workflow_id
from common.models.pmp import MessageStatus, MessageType, PMPMessage, PMPMetadata
from deliberation.manager import DeliberationManager
from deliberation.registry import DeliberationRegistry
from providers.mock_provider import MockModelProvider
from researcher.schemas.research_report import ResearchReport
from storage.deliberation_workflow_repository import DeliberationWorkflowRepository
from tests.researcher_helpers import valid_source


def make_report(
    workflow_id: str | None = None,
    *,
    evidence_count: int = 3,
    review_status: str = "approved",
) -> ResearchReport:
    workflow_id = workflow_id or new_workflow_id()
    sources = []
    categories = ["ACADEMIC", "GOVERNMENT", "INDUSTRY"]
    for index in range(evidence_count):
        category = categories[index % len(categories)]
        sources.append(
            valid_source(
                category,
                source_id=f"source_{index}",
                evidence_id=f"evidence_{index}",
                research_question_ids=["rq_employment"],
                title=f"Traceable source {index}",
                url=f"https://example.invalid/source/{index}",
            )
        )
    evidence_items = [
        {
            "evidence_id": source["evidence_id"],
            "source_id": source["source_id"],
            "research_question_ids": source["research_question_ids"],
            "summary": source["summary"],
            "stance": source["stance"],
            "directness": source["directness"],
        }
        for source in sources
    ]
    source_metadata = [
        {
            "source_id": source["source_id"],
            "source_type": source["source_type"],
            "title": source["title"],
            "source_name": source["source_name"],
            "url": source["url"],
            "author_or_organization": source["author_or_organization"],
            "published_at": source["published_at"],
            "retrieved_at": source["retrieved_at"],
            "geographic_scope": source["geographic_scope"],
            "time_scope": source["time_scope"],
            "source_specific_metadata": source["source_specific_metadata"],
        }
        for source in sources
    ]
    assessments = [
        {
            "evidence_id": source["evidence_id"],
            "source_id": source["source_id"],
            "reliability": source["reliability"],
            "directness": source["directness"],
            "primary_source": source["primary_source"],
            "limitations": source["limitations"],
        }
        for source in sources
    ]
    by_category: dict[str, list[str]] = {}
    for source in sources:
        by_category.setdefault(source["source_type"], []).append(source["source_id"])
    return ResearchReport.model_validate(
        {
            "research_report_id": "report_test",
            "workflow_id": workflow_id,
            "research_plan_id": "plan_test",
            "topic": "生成AIと雇用",
            "general_opinion": "生成AIは多くの仕事を奪う",
            "research_questions": [
                {
                    "research_question_id": "rq_employment",
                    "question": "雇用者数と仕事内容はどう変化したか",
                    "required_categories": sorted(by_category),
                    "completed_categories": sorted(by_category),
                    "evidence_ids": [item["evidence_id"] for item in sources],
                    "coverage_status": "COMPLETE",
                }
            ],
            "research_scope": ["日本", "2022年以降"],
            "sources": sources,
            "evidence_items": evidence_items,
            "source_metadata": source_metadata,
            "source_perspectives": by_category,
            "evidence_quality_assessments": assessments,
            "research_limitations": [],
            "unresolved_questions": [],
            "sources_by_category": by_category,
            "source_count_by_category": {
                category: len(source_ids) for category, source_ids in by_category.items()
            },
            "cross_source_observations": [],
            "evidence_gaps": [],
            "review": {
                "status": review_status,
                "reason": "test quality gate",
                "findings": [],
                "revision_targets": [],
            },
        }
    )


def make_deliberation_handoff(
    report: ResearchReport | None = None,
    *,
    message_type: MessageType = MessageType.RESEARCH_RESULT,
) -> PMPMessage:
    report = report or make_report()
    report_payload = report.model_dump(mode="json")
    payload = {
        **report_payload,
        "research_report": report_payload,
        "quality_review": report.review or {"status": "approved"},
        "known_limitations": report.research_limitations,
        "unresolved_gaps": [],
    }
    return PMPMessage.create(
        workflow_id=report.workflow_id,
        sender_agent_id="researcher.manager",
        receiver_agent_id="deliberation.manager",
        message_type=message_type,
        objective="Provide Research Report to Deliberation",
        payload=payload,
        metadata=PMPMetadata(status=MessageStatus.COMPLETED),
    )


def make_manager(
    data_dir: Path,
    provider: MockModelProvider | None = None,
    *,
    max_revisions: int = 2,
) -> DeliberationManager:
    provider = provider or MockModelProvider()
    return DeliberationManager(
        DeliberationRegistry(provider, {}),
        DeliberationWorkflowRepository(data_dir),
        max_revisions=max_revisions,
    )
