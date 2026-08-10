from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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
    if revision_count or upstream_count:
        lines.append(
            f"  revisions: internal={revision_count}, upstream={upstream_count}"
        )
    if data.get("error"):
        lines.append(f"  error: {data['error']}")

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
    if status in {"FAILED", "BLOCKED"}:
        return f"py main.py --status {workflow_id}（errorとmessages.jsonlを確認）"
    if status == "WAITING_UPSTREAM_REVISION":
        return "上流のrevision outboxを処理後、該当層の--*-resumeを実行"
    if layer == "producer" and status == "COMPLETED":
        return f"py main.py --researcher {workflow_id}"
    if layer == "researcher" and status == "COMPLETED":
        return f"py main.py --deliberation {workflow_id}"
    if layer == "deliberation" and status == "COMPLETED":
        return f"py main.py --conclusion {workflow_id}"
    if layer == "conclusion" and status == "WAITING_HUMAN_SELECTION":
        return f"py main.py --conclusion-select {workflow_id} <candidate_id>"
    if layer == "conclusion" and status == "COMPLETED":
        return f"py main.py --playwright {workflow_id}"
    if layer == "playwright" and status == "COMPLETED":
        return "storage/data/deliveries/<workflow_id>/ を確認"
    return None


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
