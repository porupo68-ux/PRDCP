"""Read-only production clone verification for Cycle 041 cross-layer repair flow."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import Settings
from discord_app.commands import run_conclusion, run_deliberation, run_playwright
from providers.mock_provider import MockModelProvider
from runtime import build_all_managers
from storage.researcher_workflow_repository import ResearcherWorkflowRepository


EXPECTED_REPAIR_KINDS = {
    "recognized_media_source_reclassification",
    "report_limitation_exact_deduplication",
}
EXPECTED_DELIVERY_FILES = {
    "final_script_package.json",
    "script.md",
    "citation_manifest.json",
    "source_list.md",
    "visual_plan.md",
    "production_notes.md",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_file(source: Path, target: Path) -> None:
    if not source.exists():
        raise FileNotFoundError(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def source_boundary(source_data: Path, workflow_id: str) -> list[Path]:
    paths = [
        source_data / "workflows" / "producer" / f"{workflow_id}.json",
        source_data / "workflows" / "researcher" / f"{workflow_id}.json",
        source_data / "artifacts" / "research_reports" / f"{workflow_id}.json",
        source_data / "outbox" / "researcher" / f"{workflow_id}.json",
        source_data / "outbox" / "deliberation" / f"{workflow_id}.json",
    ]
    decision_dir = source_data / "artifacts" / "human_evidence_decisions" / workflow_id
    if decision_dir.exists():
        paths.extend(sorted(path for path in decision_dir.rglob("*") if path.is_file()))
    return paths


def verify(source_project: Path, workflow_id: str) -> dict:
    source_data = source_project / "storage" / "data"
    boundary = source_boundary(source_data, workflow_id)
    source_hashes = {str(path.relative_to(source_data)): sha256(path) for path in boundary}

    with tempfile.TemporaryDirectory(prefix="prdcp-cycle041-clone-") as temporary:
        clone_data = Path(temporary)
        for source in boundary:
            copy_file(source, clone_data / source.relative_to(source_data))

        researcher_repository = ResearcherWorkflowRepository(clone_data)
        researcher_state = researcher_repository.load(workflow_id)
        if researcher_state.status != "COMPLETED":
            raise AssertionError("saved Researcher state is not COMPLETED")
        if len(researcher_state.collected_sources) != 27:
            raise AssertionError("saved Researcher evidence set does not contain 27 sources")
        if researcher_state.human_evidence_decision is None:
            raise AssertionError("saved Human Evidence Decision is missing")
        repair_kinds = {
            item.repair_kind
            for item in researcher_state.human_evidence_integrity_repairs
        }
        if repair_kinds != EXPECTED_REPAIR_KINDS:
            raise AssertionError(f"unexpected repair contract variants: {repair_kinds}")

        settings = Settings(
            provider="mock",
            discord_bot_token=None,
            openrouter_api_key=None,
            openrouter_base_url="https://openrouter.ai/api/v1",
            data_dir=clone_data,
            log_level="INFO",
            models={},
            retrieval_provider="mock",
            demo_safe_mode=True,
        )
        provider = MockModelProvider(
            reservation_root=clone_data / "provider_call_reservations"
        )
        _producer, _researcher, deliberation, conclusion, playwright = (
            build_all_managers(settings, provider=provider)
        )

        deliberation_state = asyncio.run(
            run_deliberation(deliberation, workflow_id=workflow_id)
        )
        if deliberation_state.status != "COMPLETED":
            raise AssertionError(
                f"Deliberation clone did not complete: {deliberation_state.status}"
            )
        conclusion_waiting = asyncio.run(
            run_conclusion(conclusion, workflow_id=workflow_id)
        )
        if conclusion_waiting.status != "WAITING_HUMAN_SELECTION":
            raise AssertionError(
                "Conclusion clone did not reach the Human Selection boundary"
            )
        selected_id = conclusion_waiting.position_candidates[0][
            "position_candidate_id"
        ]
        conclusion_state = conclusion.select(workflow_id, [selected_id])
        if conclusion_state.status != "COMPLETED":
            raise AssertionError("Conclusion clone selection did not complete")
        playwright_state = asyncio.run(
            run_playwright(playwright, workflow_id=workflow_id)
        )
        if playwright_state.status != "COMPLETED":
            raise AssertionError("Playwright clone did not complete")

        delivery_dir = clone_data / "deliveries" / workflow_id
        delivery_files = {
            path.name for path in delivery_dir.iterdir() if path.is_file()
        }
        if delivery_files != EXPECTED_DELIVERY_FILES:
            raise AssertionError(f"unexpected Delivery files: {delivery_files}")

        final = conclusion_state.final_conclusion
        production_context = playwright_state.production_context
        if final["human_evidence_decision"] != production_context[
            "human_evidence_decision"
        ]:
            raise AssertionError("Human Evidence Decision changed downstream")
        if final["accepted_evidence_gaps"] != production_context[
            "accepted_evidence_gaps"
        ]:
            raise AssertionError("accepted Evidence Gaps changed downstream")

        result = {
            "workflow_id": workflow_id,
            "researcher_status": researcher_state.status,
            "researcher_source_count": len(researcher_state.collected_sources),
            "repair_kinds": sorted(repair_kinds),
            "human_evidence_decision": researcher_state.human_evidence_decision.decision,
            "deliberation_status": deliberation_state.status,
            "conclusion_gate_status": conclusion_waiting.status,
            "conclusion_status": conclusion_state.status,
            "playwright_status": playwright_state.status,
            "delivery_files": sorted(delivery_files),
            "mock_provider_calls": len(provider.calls),
            "real_provider_calls": 0,
            "retrieval_calls": 0,
        }

    after_hashes = {str(path.relative_to(source_data)): sha256(path) for path in boundary}
    if source_hashes != after_hashes:
        raise AssertionError("production source boundary changed during clone verification")
    result["source_storage_mutated"] = False
    result["source_hashes"] = source_hashes
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-project", type=Path, required=True)
    parser.add_argument("--workflow-id", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            verify(args.source_project.resolve(), args.workflow_id),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
