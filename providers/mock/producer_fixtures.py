from __future__ import annotations

from datetime import datetime, timezone

from common.ids import new_id


def topic_candidates(input_data: dict) -> dict:
    requested = (input_data.get("user_topic") or input_data.get("search_query") or "").strip()
    candidates: list[dict] = []
    if requested:
        candidates.append(
            {
                "topic_id": new_id("topic"),
                "title": requested,
                "summary": "ユーザー指定テーマを検証候補として登録",
                "source": "user",
                "url": "https://example.invalid/mock/user-topic",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    defaults = [
        ("生成AIは人間の仕事を奪うのか", "AIと雇用の関係に関する議論", "mock-news"),
        ("リモートワークは生産性を高めるのか", "働き方と生産性に関する議論", "mock-video"),
        ("スマートシティは生活の摩擦を減らすのか", "都市技術と公共性に関する議論", "mock-social"),
    ]
    existing_titles = {item["title"] for item in candidates}
    for title, summary, source in defaults:
        if title in existing_titles:
            continue
        candidates.append(
            {
                "topic_id": new_id("topic"),
                "title": title,
                "summary": summary,
                "source": source,
                "url": f"https://example.invalid/mock/{len(candidates) + 1}",
                "published_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    max_candidates = input_data.get("search_constraints", {}).get("max_candidates", 3)
    selected = candidates[:max_candidates]
    if not any("ai" in item["title"].lower() for item in selected):
        selected[-1] = candidates[
            next(i for i, item in enumerate(candidates) if "ai" in item["title"].lower())
        ]
    return {"topic_candidates": selected}


def general_opinion(input_data: dict) -> dict:
    topic = input_data["selected_topic"]
    title = topic["title"]
    if "AI" in title.upper() or "生成AI" in title:
        statement = "生成AIの普及により、多くの人間の仕事が失われる"
    else:
        statement = f"{title}は社会へ大きな影響を与えるという見方が広く共有されている"
    return {
        "general_opinion": {
            "general_opinion_id": new_id("opinion"),
            "statement": statement,
            "confidence": 0.75,
            "evidence_summary": "ニュース、動画、SNSで繰り返し見られる主張をモックとして整理",
            "supporting_sources": [
                {"source": "mock-news", "url": "https://example.invalid/mock/news"},
                {"source": "mock-video", "url": "https://example.invalid/mock/video"},
                {"source": "mock-social", "url": "https://example.invalid/mock/social"},
            ],
        }
    }


def research_plan(input_data: dict) -> dict:
    topic = input_data["selected_topic"]
    opinion = input_data["general_opinion"]
    revised = input_data.get("revision_context") is not None
    title = topic["title"]
    is_ai_topic = "AI" in title.upper() or "生成AI" in title
    scope = [
        "日本",
        "2022年以降",
        "生成AIによる直接的・間接的な雇用影響"
        if is_ai_topic
        else f"{title}に直接関連する社会的影響",
    ]
    if revised:
        scope.append("産業別・職種別の差を区別する")
    first_question = (
        "生成AIの導入は雇用者数と仕事内容にどのような影響を与えているか"
        if is_ai_topic
        else f"{title}に関連する観測可能な結果はどのように変化しているか"
    )
    second_question = (
        "専門家、政治家、報道、市民はその影響をどのように認識しているか"
        if is_ai_topic
        else f"専門家、政治家、報道、市民は{title}をどのように認識しているか"
    )
    return {
        "research_plan": {
            "research_plan_id": new_id("plan"),
            "topic_id": topic["topic_id"],
            "topic": topic["title"],
            "general_opinion_id": opinion["general_opinion_id"],
            "general_opinion": opinion["statement"],
            "research_questions": [
                {
                    "research_question_id": new_id("rq"),
                    "question": first_question,
                    "research_targets": ["ACADEMIC", "GOVERNMENT", "INDUSTRY"],
                },
                {
                    "research_question_id": new_id("rq"),
                    "question": second_question,
                    "research_targets": ["EXPERT", "POLITICIAN", "NEWS", "PUBLIC_OPINION"],
                },
            ],
            "scope": scope,
            "constraints": [
                "一般論の真偽を事前に決定しない",
                "一次情報を優先する",
                "予測と観測済み事実を区別する",
            ],
        }
    }


def quality_review(input_data: dict, decision: str) -> dict:
    plan = input_data["research_plan"]
    if decision == "revision_required":
        return {
            "status": "revision_required",
            "revision_target": "producer.research_planner",
            "reason": "Research Questionの調査範囲をさらに具体化する必要がある",
            "required_action": "対象地域、期間、産業別・職種別の区分を明示する",
            "approved_research_plan": None,
        }
    if decision == "blocked":
        return {
            "status": "blocked",
            "revision_target": None,
            "reason": "自動修正できない前提条件が不足している",
            "required_action": None,
            "approved_research_plan": None,
        }
    return {
        "status": "approved",
        "revision_target": None,
        "reason": "Topic、一般論、質問、範囲、制約がResearcherの開始条件を満たしている",
        "required_action": None,
        "approved_research_plan": plan,
    }
