from __future__ import annotations

import json

from deliberation.state import DeliberationWorkflowState
from deliberation.workflow import COUNTERARGUMENT_ANALYST_ID
from deliberation.workflow import DISPLAY_NAMES as DELIBERATION_DISPLAY_NAMES
from deliberation.workflow import PRIMARY_ANALYST_IDS as DELIBERATION_PRIMARY_IDS
from deliberation.workflow import QUALITY_REVIEWER_ID as DELIBERATION_QR_ID
from producer.state import ProducerWorkflowState
from producer.workflow import AGENT_ORDER, DISPLAY_NAMES
from researcher.state import ResearcherWorkflowState
from researcher.workflow import DISPLAY_NAMES as RESEARCHER_DISPLAY_NAMES
from researcher.workflow import QUALITY_REVIEWER_ID
from conclusion.state import ConclusionWorkflowState
from conclusion.workflow import DISPLAY_NAMES as CONCLUSION_DISPLAY_NAMES
from conclusion.workflow import (
    DECISION_EVALUATOR_ID,
    DECISION_INTEGRATOR_ID,
    POSITION_GENERATOR_ID,
    QUALITY_REVIEWER_ID as CONCLUSION_QR_ID,
)
from playwright.state import PlaywrightWorkflowState
from playwright.workflow import AGENT_ORDER as PLAYWRIGHT_AGENT_ORDER
from playwright.workflow import DISPLAY_NAMES as PLAYWRIGHT_DISPLAY_NAMES


DISCORD_MESSAGE_LIMIT = 2000


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    if text.startswith("```json\n") and text.endswith("\n```"):
        body = text[len("```json\n") : -len("\n```")]
        wrapped_limit = limit - len("```json\n\n```")
        return [f"```json\n{part}\n```" for part in split_message(body, wrapped_limit)]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def format_status(state: ProducerWorkflowState) -> str:
    completed = set(state.completed_agents)
    lines = [
        f"Workflow: {state.workflow_id}",
        f"Status: {state.status}",
        f"Current Agent: {state.current_agent_id or '-'}",
        f"Revision Count: {state.revision_count} / 3",
        "",
        "Completed:",
    ]
    for agent_id in AGENT_ORDER:
        marker = "✓" if agent_id in completed else ("→" if state.current_agent_id == agent_id else "·")
        lines.append(f"{marker} {DISPLAY_NAMES[agent_id]}")
    if state.error:
        lines.extend(["", f"Error: {state.error.get('message', state.error)}"])
    return "\n".join(lines)


def format_result(state: ProducerWorkflowState) -> str:
    result = state.final_result()
    if result is None:
        return format_status(state)
    return "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"


def format_researcher_status(state: ResearcherWorkflowState) -> str:
    completed = set(state.completed_agents)
    failed = set(state.failed_agents)
    current = set(state.current_agent_ids)
    task_agents = list(
        dict.fromkeys(task["target_agent_id"] for task in state.research_tasks)
    )
    lines = [
        f"Researcher Workflow: {state.workflow_id}",
        f"Status: {state.status}",
        f"Research Questions: {len(state.research_plan.get('research_questions', []))}",
        f"Sources: {len(state.collected_sources)}",
        f"Revision: {state.revision_count} / 3",
        "",
        "Agents:",
    ]
    for agent_id in task_agents + [QUALITY_REVIEWER_ID]:
        if agent_id == QUALITY_REVIEWER_ID:
            done = state.review_result is not None
        else:
            done = agent_id in completed
        marker = "✓" if done else "×" if agent_id in failed else "→" if agent_id in current else "·"
        lines.append(f"{marker} {RESEARCHER_DISPLAY_NAMES[agent_id]}")
    if state.error:
        lines.extend(["", f"Error: {state.error.get('message', state.error)}"])
    return "\n".join(lines)


def format_researcher_result(state: ResearcherWorkflowState) -> str:
    result = state.final_result()
    if result is None:
        return format_researcher_status(state)
    return "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"


def format_researcher_sources(report) -> str:
    """Format a Research Report as the compact #sources audit view."""
    data = report.model_dump(mode="json") if hasattr(report, "model_dump") else report
    sources = data.get("sources", []) if data else []
    lines = ["Sources", f"Workflow: {data.get('workflow_id', '-') if data else '-'}"]
    if not sources:
        return "\n".join([*lines, "", "No sources were recorded."])

    categories: dict[str, list[dict]] = {}
    for source in sources:
        categories.setdefault(str(source.get("source_type", "OTHER")), []).append(source)

    for category, category_sources in categories.items():
        lines.extend(["", f"[{category.title().replace('_', ' ')}]"])
        for source in category_sources:
            lines.extend(
                [
                    str(source.get("source_id", "-")),
                    f"Title: {source.get('title', '-')}",
                    f"URL: {source.get('url', '-')}",
                    f"Researcher: {source.get('source_name', '-')}",
                    f"Evidence IDs: {source.get('evidence_id', '-')}",
                    "",
                ]
            )
    return "\n".join(lines).rstrip()


def format_deliberation_status(state: DeliberationWorkflowState) -> str:
    completed = set(state.completed_agents)
    failed = set(state.failed_agents)
    current = set(state.current_agent_ids)
    final = state.final_integration or {}
    lines = [
        f"Deliberation Workflow: {state.workflow_id}",
        f"Status: {state.status}",
        "",
        "Primary Analysis:",
    ]
    for agent_id in DELIBERATION_PRIMARY_IDS:
        marker = "✓" if agent_id in completed else "×" if agent_id in failed else "→" if agent_id in current else "·"
        lines.append(f"{marker} {DELIBERATION_DISPLAY_NAMES[agent_id]}")
    lines.extend(
        [
            "",
            "Integration:",
            ("✓" if state.initial_integration else "·") + " Initial Integration",
            ("✓" if state.counterargument_analysis else "·")
            + f" {DELIBERATION_DISPLAY_NAMES[COUNTERARGUMENT_ANALYST_ID]}",
            ("✓" if state.final_integration else "·") + " Final Integration",
            "",
            "Quality Review:",
            ("✓" if state.review_result else "→" if DELIBERATION_QR_ID in current else "·")
            + f" {DELIBERATION_DISPLAY_NAMES[DELIBERATION_QR_ID]}",
            "",
            f"Viewpoints: {len(final.get('major_viewpoints', []))}",
            f"Claims: {len(final.get('key_claims', []))}",
            f"Revision: {state.revision_count} / 2",
            f"Upstream Revision: {state.upstream_revision_count}",
        ]
    )
    if state.error:
        lines.extend(["", f"Error: {state.error.get('message', state.error)}"])
    return "\n".join(lines)


def format_deliberation_result(state: DeliberationWorkflowState) -> str:
    result = state.final_result()
    if result is None:
        return format_deliberation_status(state)
    return "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"


def format_conclusion_status(state: ConclusionWorkflowState) -> str:
    completed = set(state.completed_agents)
    failed = set(state.failed_agents)
    current = set(state.current_agent_ids)
    viable = (state.decision_integration or {}).get("viable_candidates", [])
    blocked = (state.decision_integration or {}).get("excluded_candidates", [])
    lines = [
        f"Conclusion Workflow: {state.workflow_id}",
        f"Status: {state.status}",
        "",
        "Agents:",
    ]
    for agent_id in (
        POSITION_GENERATOR_ID,
        DECISION_EVALUATOR_ID,
        DECISION_INTEGRATOR_ID,
        CONCLUSION_QR_ID,
    ):
        marker = "✓" if agent_id in completed else "×" if agent_id in failed else "→" if agent_id in current else "·"
        lines.append(f"{marker} {CONCLUSION_DISPLAY_NAMES[agent_id]}")
    lines.extend(
        [
            "",
            f"Candidates: {len(state.position_candidates)}",
            f"Viable: {len(viable)}",
            f"Blocked: {len(blocked)}",
            f"Limitations: {len(state.limitations)}",
            f"Revision: {state.revision_count} / 2",
            f"Upstream Revision: {state.upstream_revision_count}",
        ]
    )
    if state.error:
        lines.extend(["", f"Error: {state.error.get('message', state.error)}"])
    return "\n".join(lines)


def format_conclusion_options(state: ConclusionWorkflowState) -> str:
    if not state.conclusion_package:
        return format_conclusion_status(state)
    package = state.conclusion_package
    lines = [
        f"Conclusion Options: {state.workflow_id}",
        f"Question: {package['decision_question']}",
        "",
    ]
    recommended = (package.get("primary_recommendation") or {}).get("candidate_id")
    for index, option in enumerate(package["options"], start=1):
        candidate_id = option["position_candidate_id"]
        marker = " [recommended]" if candidate_id == recommended else ""
        lines.extend(
            [
                f"{index}. {option['title']}{marker}",
                f"   ID: {candidate_id}",
                f"   {option['summary']}",
            ]
        )
    if package.get("integrated_option"):
        lines.extend(["", "Integrated option available: !conclusion_integrate で再統合できます"])
    return "\n".join(lines)


def format_conclusion_result(state: ConclusionWorkflowState) -> str:
    result = state.final_result()
    if result is None:
        return format_conclusion_status(state)
    return "```json\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n```"


def format_playwright_status(state: PlaywrightWorkflowState) -> str:
    completed = set(state.completed_agents)
    failed = set(state.failed_agents)
    current = set(state.current_agent_ids)
    script = state.citation_validated_script or state.script_draft or {}
    sections = script.get("sections", [])
    paragraphs = [paragraph for section in sections for paragraph in section.get("paragraphs", [])]
    mappings = (state.citation_manifest or {}).get("mappings", [])
    citation_required = sum(1 for paragraph in paragraphs if paragraph.get("citation_required"))
    gate_done = state.final_gate_result is not None
    lines = [
        f"Playwright Workflow: {state.workflow_id}",
        f"Status: {state.status}",
        "",
        "Agents:",
    ]
    for agent_id in PLAYWRIGHT_AGENT_ORDER:
        marker = "✓" if agent_id in completed else "×" if agent_id in failed else "→" if agent_id in current else "○"
        lines.append(f"{marker} {PLAYWRIGHT_DISPLAY_NAMES[agent_id]}")
    lines.append(("✓" if gate_done else "○") + " Final Gate")
    lines.extend(
        [
            "",
            f"Sections: {len(sections)}",
            f"Paragraphs: {len(paragraphs)}",
            f"Citations mapped: {len(mappings)} / {citation_required}",
            f"Revision: {state.revision_count} / 2",
            f"Upstream Revision: {state.upstream_revision_count}",
        ]
    )
    if state.error:
        lines.extend(["", f"Error: {state.error.get('message', state.error)}"])
    return "\n".join(lines)


def format_playwright_result(state: PlaywrightWorkflowState) -> str:
    package = state.final_script_package
    if package is None:
        return format_playwright_status(state)
    summary = package.get("production_summary", {})
    duration = int(summary.get("estimated_duration_seconds", 0))
    minutes, seconds = divmod(duration, 60)
    lines = [
        "Playwright completed",
        "",
        f"Duration: {minutes}分{seconds:02d}秒",
        f"Sections: {summary.get('section_count', 0)}",
        f"Paragraphs: {summary.get('paragraph_count', 0)}",
        f"Citations: {summary.get('citation_count', 0)}",
        f"Visual cues: {summary.get('visual_cue_count', 0)}",
        f"Charts: {summary.get('chart_request_count', 0)}",
        f"Limitations: {len(package.get('limitations_to_disclose', []))}",
    ]
    if state.delivery_paths:
        lines.extend(["", "Delivery files:"])
        lines.extend(f"- {name}: {path}" for name, path in state.delivery_paths.items())
    return "\n".join(lines)
