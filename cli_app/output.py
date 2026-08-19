from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from playwright.deterministic_repair import (
    MAX_DETERMINISTIC_REPAIR_PASSES,
    REPAIRABLE_FINDING_CODES,
)


LAYER_LABELS = {
    "producer": "Producer",
    "researcher": "Researcher",
    "deliberation": "Deliberation",
    "conclusion": "Conclusion",
    "playwright": "Playwright",
}


def state_to_dict(state: Any) -> dict[str, Any]:
    if isinstance(state, dict):
        return state
    return state.model_dump(mode="json")


def print_state(
    layer: str,
    state: Any,
    *,
    json_output: bool = False,
    include_next_action: bool = True,
) -> None:
    data = state_to_dict(state)
    if json_output:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(format_state_summary(layer, data, include_next_action=include_next_action))


def format_state_summary(
    layer: str,
    data: dict[str, Any],
    *,
    include_next_action: bool = True,
) -> str:
    label = LAYER_LABELS.get(layer, layer.title())
    workflow_id = data.get("workflow_id", "-")
    status = data.get("status", "UNKNOWN")
    current = data.get("current_agent_id") or data.get("current_agent_ids") or []
    if isinstance(current, str):
        current = [current]
    completed = data.get("completed_agents") or []
    failed = data.get("failed_agents") or []
    lines = [f"[{label}] {status}", f"  workflow_id: {workflow_id}"]
    if current:
        lines.append("  current: " + ", ".join(current))
    lines.append(f"  completed agents: {len(completed)}")
    if failed:
        lines.append("  failed agents: " + ", ".join(failed))
    revision_count = int(data.get("revision_count") or 0)
    upstream_count = int(data.get("upstream_revision_count") or 0)
    external_count = int(data.get("external_revision_count") or 0)
    if revision_count or upstream_count or external_count:
        lines.append(
            f"  revisions: internal={revision_count}, upstream={upstream_count}, "
            f"external={external_count}"
        )
    deterministic_repair_count = int(data.get("deterministic_repair_count") or 0)
    if deterministic_repair_count:
        lines.append(f"  deterministic repairs: {deterministic_repair_count}")
    if data.get("external_revision_status"):
        lines.append(f"  external checkpoint: {data['external_revision_status']}")
    if data.get("error"):
        lines.append(f"  error: {data['error']}")

    if layer == "researcher":
        report = data.get("research_report") or {}
        decision = data.get("human_evidence_decision") or {}
        accepted = data.get("accepted_evidence_gaps") or []
        if report:
            lines.append(f"  sources: {len(report.get('sources') or [])}")
        if decision:
            lines.append(
                "  human evidence decision: " + str(decision.get("decision", "-"))
            )
            lines.append(f"  accepted evidence gaps: {len(accepted)}")

    if layer == "conclusion" and status == "WAITING_HUMAN_SELECTION":
        candidates = data.get("position_candidates") or []
        lines.append("  candidates:")
        for candidate in candidates:
            lines.append(
                f"    - {candidate.get('position_candidate_id', '?')}: "
                f"{candidate.get('title', '(untitled)')}"
            )
    if layer == "playwright" and data.get("delivery_paths"):
        lines.append("  deliveries:")
        for name, path in sorted(data["delivery_paths"].items()):
            lines.append(f"    - {name}: {path}")

    next_action = next_action_for(layer, data) if include_next_action else None
    if next_action:
        lines.append(f"  next: {next_action}")
    return "\n".join(lines)


def next_action_for(layer: str, data: dict[str, Any]) -> str | None:
    workflow_id = data.get("workflow_id", "<workflow_id>")
    status = data.get("status", "")
    if layer == "researcher" and status == "WAITING_HUMAN_EVIDENCE_REVIEW":
        return (
            f"py main.py --researcher-evidence {workflow_id}（確認後に"
            "--researcher-accept / --researcher-accept-limitations / "
            "--researcher-revise）"
        )
    if layer == "researcher" and status == "BLOCKED":
        error = data.get("error") or {}
        if error.get("code") == "EVIDENCE_REVISION_PROVIDER_AUTHORIZATION_REQUIRED":
            return "Evidence Revision Planを確認し、Provider実行は別途明示承認"
        if data.get("review_result") and not data.get("human_evidence_decision"):
            return (
                f"py main.py --researcher-recover {workflow_id}（旧Quality Reviewを"
                "Human Evidence Gateへ0-callで移行後、--researcher-evidenceで確認）"
            )
    if layer == "conclusion" and status == "BLOCKED":
        review = data.get("review_result") or {}
        if (
            review.get("status") == "revision_required"
            and review.get("revision_scope") != "deliberation_return"
            and review.get("revision_targets")
        ):
            return (
                f"py main.py --conclusion-revise {workflow_id} --safe-mode"
            )
    if layer == "playwright" and status == "BLOCKED":
        gate = data.get("final_gate_result") or {}
        validation = data.get("deterministic_validation") or {}
        internal_errors = [
            item
            for item in validation.get("findings") or []
            if isinstance(item, dict)
            and item.get("severity") == "ERROR"
            and item.get("target_agent_id", "").startswith("playwright.")
            and not item.get("upstream_required")
        ]
        repairable_only = bool(internal_errors) and all(
            item.get("code") in REPAIRABLE_FINDING_CODES
            for item in internal_errors
        )
        if repairable_only:
            if (
                int(data.get("deterministic_repair_count") or 0)
                < MAX_DETERMINISTIC_REPAIR_PASSES
            ):
                return f"py main.py --playwright-recover {workflow_id} --safe-mode"
            return f"py main.py --status {workflow_id}（deterministic repair上限到達）"
        has_internal_errors = any(
            item.get("severity") == "ERROR"
            and item.get("target_agent_id", "").startswith("playwright.")
            and not item.get("upstream_required")
            for item in validation.get("findings") or []
            if isinstance(item, dict)
        )
        if gate.get("status") == "REVISION_REQUIRED" or has_internal_errors:
            return f"py main.py --playwright-revise {workflow_id} --safe-mode"
    if status in {"FAILED", "BLOCKED"}:
        return f"py main.py --status {workflow_id}（errorとmessages.jsonlを確認）"
    if status == "WAITING_UPSTREAM_REVISION":
        return "上流のrevision outboxを処理後、該当層の--*-resumeを実行"
    if layer == "producer" and status == "COMPLETED":
        return f"py main.py --researcher {workflow_id}"
    if layer == "researcher" and status == "COMPLETED":
        return f"py main.py --deliberation {workflow_id}"
    if layer == "researcher" and status == "COMPLETED_REVISION":
        return f"py main.py --deliberation-resume {workflow_id}"
    if layer == "deliberation" and status == "COMPLETED":
        return f"py main.py --conclusion {workflow_id}"
    if layer == "conclusion" and status == "WAITING_HUMAN_SELECTION":
        return f"py main.py --conclusion-select {workflow_id} <candidate_id>"
    if layer == "conclusion" and status == "COMPLETED":
        return f"py main.py --playwright {workflow_id}"
    if layer == "playwright" and status == "COMPLETED":
        return "storage/data/deliveries/<workflow_id>/ を確認"
    return None


def format_human_evidence_summary(summary: Any) -> str:
    data = state_to_dict(summary)
    hard = data.get("hard_integrity_findings") or []
    gaps = data.get("evidence_sufficiency_findings") or []
    resolved = data.get("resolved_integrity_findings") or []
    unknown = data.get("unclassified_findings") or []
    lines = [
        "Researcher Evidence Review",
        f"Workflow: {data.get('workflow_id', '-')}",
        f"Research Report Sources: {data.get('source_count', 0)}",
        f"Quality Review: {data.get('quality_review_status', '-')}",
        f"Hard Integrity Findings: {len(hard)}",
        f"Resolved Integrity Findings: {len(resolved)}",
        f"Unclassified Findings: {len(unknown)}",
        f"Evidence Sufficiency Findings: {len(gaps)}",
        f"Quality Reviewer Recommendation: {data.get('recommended_action', '-')}",
        f"Human Gate Eligible: {'YES' if data.get('eligible') else 'NO'}",
    ]
    if gaps:
        lines.extend(["", "Evidence Gaps:"])
        lines.extend(
            f"  - {item['finding_id']}: {item['issue']}" for item in gaps
        )
    if hard or unknown:
        lines.extend(["", "Non-overridable findings:"])
        lines.extend(
            f"  - {item['finding_id']}: {item['issue']}" for item in hard + unknown
        )
    plan = data.get("revision_plan")
    if plan:
        lines.extend(
            [
                "",
                "Evidence Revision Plan:",
                f"  Retrieval <= {plan['estimated_max_retrieval_calls']}",
                f"  Reasoning <= {plan['estimated_max_reasoning_calls']}",
                f"  Quality Review = {plan['estimated_quality_review_calls']}",
                f"  Maximum Provider calls = {plan['estimated_max_provider_calls']}",
                "  Explicit Provider authorization required: YES",
            ]
        )
    if data.get("eligible") and not data.get("existing_decision"):
        lines.extend(
            [
                "",
                "Human decision required:",
                "  ACCEPT",
                "  ACCEPT_WITH_LIMITATIONS",
                "  REVISE",
                "Provider calls for this decision: 0",
            ]
        )
    return "\n".join(lines)


def load_workflow_states(data_dir: Path, workflow_id: str) -> list[tuple[str, dict[str, Any]]]:
    states: list[tuple[str, dict[str, Any]]] = []
    for layer in LAYER_LABELS:
        path = data_dir / "workflows" / layer / f"{workflow_id}.json"
        if layer == "producer" and not path.exists():
            legacy = data_dir / "workflows" / f"{workflow_id}.json"
            path = legacy if legacy.exists() else path
        if not path.exists():
            continue
        states.append((layer, json.loads(path.read_text(encoding="utf-8"))))
    return states
