from __future__ import annotations

from datetime import datetime, timezone

from common.ids import new_id


def research_result(
    input_data: dict,
    *,
    no_result_agent_ids: set[str],
) -> dict:
    agent_id = input_data["target_agent_id"]
    if agent_id in no_result_agent_ids:
        return {
            "task_id": input_data["task_id"],
            "research_question_id": input_data["research_question_id"],
            "agent_id": agent_id,
            "sources": [],
            "search_summary": "指定範囲で有効な情報源を取得できなかった",
            "coverage_status": "NO_RESULT",
            "limitations": ["Mock scenario: no source found"],
        }

    category = input_data["research_target"]
    revision = input_data.get("revision_context") is not None
    category_slug = category.lower()
    rq_id = input_data["research_question_id"]
    suffix = "revision" if revision else "initial"
    now = datetime.now(timezone.utc)
    details = source_details(category, revision)
    retrieved_sources = input_data.get("retrieval_context", {}).get("sources", [])
    retrieved = retrieved_sources[0] if retrieved_sources else None
    source = {
        "source_id": retrieved["source_id"] if retrieved else new_id("source"),
        "evidence_id": new_id("evidence"),
        "research_question_ids": [rq_id],
        "source_type": category,
        "title": retrieved["title"] if retrieved else details["title"],
        "source_name": details["source_name"],
        "url": (
            retrieved["url"]
            if retrieved
            else f"https://example.invalid/research/{category_slug}/{rq_id}/{suffix}"
        ),
        "author_or_organization": details["author_or_organization"],
        "published_at": now.isoformat(),
        "retrieved_at": now.isoformat(),
        "summary": (
            f"{input_data['question']}に関連する{category}資料を、結論を付けずに整理したモック要約"
        ),
        "relevant_excerpt": retrieved["content"] if retrieved else None,
        "stance": details["stance"],
        "reliability": details["reliability"],
        "directness": details["directness"],
        "primary_source": details["primary_source"],
        "geographic_scope": [input_data["scope"][0]],
        "time_scope": input_data["scope"][1] if len(input_data["scope"]) > 1 else None,
        "limitations": details["limitations"],
        "source_specific_metadata": details["metadata"],
    }
    return {
        "task_id": input_data["task_id"],
        "research_question_id": rq_id,
        "agent_id": agent_id,
        "sources": [source],
        "search_summary": f"{category}カテゴリから1件の検証可能な資料を収集",
        "coverage_status": "COMPLETE",
        "limitations": [],
    }


def source_details(category: str, revision: bool) -> dict:
    extra = "（追加調査）" if revision else ""
    common = {
        "reliability": "MEDIUM",
        "directness": "DIRECT",
        "primary_source": True,
        "limitations": [],
    }
    by_category = {
        "EXPERT": {
            "title": f"専門家インタビュー記録{extra}",
            "source_name": "Mock Research Institute",
            "author_or_organization": "Mock Expert",
            "stance": "MIXED",
            "metadata": {
                "expert_name": "Mock Expert",
                "field": "technology and labour",
                "affiliation": "Mock Research Institute",
                "statement_context": "public interview",
            },
        },
        "ACADEMIC": {
            "title": f"Technology adoption and labour outcomes{extra}",
            "source_name": "Mock Academic Journal",
            "author_or_organization": "Mock University",
            "stance": "SUPPORTS",
            "reliability": "HIGH",
            "metadata": {
                "doi": f"10.0000/mock.{1 if revision else 0}",
                "peer_reviewed": True,
                "journal_name": "Mock Academic Journal",
                "study_type": "OBSERVATIONAL",
            },
        },
        "GOVERNMENT": {
            "title": f"Official labour statistics report{extra}",
            "source_name": "Mock Statistics Bureau",
            "author_or_organization": "Mock Government",
            "stance": "NEUTRAL",
            "reliability": "HIGH",
            "metadata": {
                "organization": "Mock Statistics Bureau",
                "country": "Japan",
                "document_type": "STATISTICS",
            },
        },
        "NEWS": {
            "title": f"Technology and work: reported changes{extra}",
            "source_name": "Mock News Agency",
            "author_or_organization": "Mock News Agency",
            "stance": "MIXED",
            "primary_source": False,
            "metadata": {"media_name": "Mock News Agency", "article_type": "REPORTING"},
        },
        "PUBLIC_OPINION": {
            "title": f"Public discussion sample{extra}",
            "source_name": "Mock Forum",
            "author_or_organization": None,
            "stance": "MIXED",
            "reliability": "LOW",
            "directness": "CONTEXTUAL",
            "primary_source": False,
            "limitations": ["This sample is not statistically representative"],
            "metadata": {
                "platform": "FORUM",
                "engagement_count": 120,
                "sample_size": None,
                "representativeness_warning": True,
            },
        },
        "POLITICIAN": {
            "title": f"Official policy statement{extra}",
            "source_name": "Mock Parliament",
            "author_or_organization": "Mock Politician",
            "stance": "SUPPORTS",
            "metadata": {
                "politician_name": "Mock Politician",
                "party": "Mock Party",
                "position": "Member of Parliament",
                "statement_type": "PARLIAMENT",
            },
        },
        "INDUSTRY": {
            "title": f"Industry technology adoption survey{extra}",
            "source_name": "Mock Industry Association",
            "author_or_organization": "Mock Industry Association",
            "stance": "OPPOSES",
            "metadata": {
                "organization_name": "Mock Industry Association",
                "organization_type": "INDUSTRY_ASSOCIATION",
                "industry": "technology",
            },
        },
    }
    return {**common, **by_category[category]}


def quality_review(input_data: dict, decision: str | None) -> dict:
    report = input_data["research_report"]
    missing = [
        (question["research_question_id"], category)
        for question in report["research_questions"]
        for category in question["required_categories"]
        if category not in question["completed_categories"]
    ]
    if decision is None:
        if missing and not report["research_limitations"]:
            decision = "revision_required"
        elif report["research_limitations"] or missing:
            decision = "approved_with_conditions"
        else:
            decision = "approved"

    if decision == "revision_required":
        rq_id, category = missing[0] if missing else default_revision_target(report)
        target_agent = {
            "EXPERT": "researcher.expert_researcher",
            "ACADEMIC": "researcher.academic_researcher",
            "GOVERNMENT": "researcher.government_researcher",
            "NEWS": "researcher.news_researcher",
            "PUBLIC_OPINION": "researcher.public_opinion_researcher",
            "POLITICIAN": "researcher.politician_researcher",
            "INDUSTRY": "researcher.industry_researcher",
        }[category]
        return {
            "status": "revision_required",
            "reason": f"{rq_id}に対する{category} evidenceを追加確認する必要がある",
            "findings": [
                {
                    "finding_id": new_id("finding"),
                    "finding_type": "EVIDENCE_SUFFICIENCY_FINDING",
                    "severity": "MAJOR",
                    "research_question_id": rq_id,
                    "target_agent_id": target_agent,
                    "issue": f"{category} source coverage is insufficient",
                    "required_action": f"Collect one additional traceable {category} source",
                }
            ],
            "revision_targets": [target_agent],
            "approved_research_report": None,
        }
    if decision == "blocked":
        return {
            "status": "blocked",
            "reason": "重大な必須Evidenceを取得できず、自動修正を継続できない",
            "findings": [],
            "revision_targets": [],
            "approved_research_report": None,
        }
    # A disclosed limitation is not itself a request for more evidence. Concrete
    # evidence deficiencies use revision_required and a routed specialist target.
    findings = []
    return {
        "status": decision,
        "reason": (
            "追跡可能性を保ったまま、制約付きでDeliberationが分析を開始できる"
            if decision == "approved_with_conditions"
            else "必要カテゴリ、Source、IDの追跡性がResearch Planを満たしている"
        ),
        "findings": findings,
        "revision_targets": [],
        "approved_research_report": report,
    }


def default_revision_target(report: dict) -> tuple[str, str]:
    for question in report["research_questions"]:
        if "GOVERNMENT" in question["required_categories"]:
            return question["research_question_id"], "GOVERNMENT"
    question = report["research_questions"][0]
    return question["research_question_id"], question["required_categories"][0]
