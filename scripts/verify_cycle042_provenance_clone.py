"""Read-only production-state clone for Cycle 042 provenance recovery."""

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
from discord_app.commands import run_conclusion, run_playwright
from providers.mock_provider import MockModelProvider
from runtime import build_all_managers
from storage.researcher_workflow_repository import ResearcherWorkflowRepository


EXPECTED_ACCEPTED_GAP = "fqrq_ai_emp_003_news_missing"
EXPECTED_DELIVERY_FILES = {
    "citation_manifest.json",
    "final_script_package.json",
    "production_notes.md",
    "script.md",
    "source_list.md",
    "visual_plan.md",
}
PRIMARY_AGENT_IDS = {
    "deliberation.argument_analyst",
    "deliberation.causal_structural_analyst",
    "deliberation.stakeholder_response_analyst",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workflow_boundary(source_data: Path, workflow_id: str) -> list[Path]:
    return sorted(
        path
        for path in source_data.rglob("*")
        if path.is_file() and workflow_id in str(path.relative_to(source_data))
    )


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def verify(source_project: Path, workflow_id: str) -> dict:
    source_data = source_project / "storage" / "data"
    boundary = workflow_boundary(source_data, workflow_id)
    source_hashes = {
        str(path.relative_to(source_data)): sha256(path) for path in boundary
    }
    source_provider_reservations = sum(
        "provider_call_reservations" in path.parts for path in boundary
    )
    source_retrieval_reservations = sum(
        "retrieval_call_reservations" in path.parts for path in boundary
    )

    with tempfile.TemporaryDirectory(prefix="prdcp-cycle042-clone-") as temporary:
        clone_data = Path(temporary)
        for source in boundary:
            copy_file(source, clone_data / source.relative_to(source_data))

        researcher_repository = ResearcherWorkflowRepository(clone_data)
        researcher_state = researcher_repository.load(workflow_id)
        if researcher_state.status != "COMPLETED":
            raise AssertionError("saved Researcher state is not COMPLETED")
        accepted_gap_ids = {
            item.finding_id for item in researcher_state.accepted_evidence_gaps
        }
        if EXPECTED_ACCEPTED_GAP not in accepted_gap_ids:
            raise AssertionError("the accepted unresolved news gap was not preserved")

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

        failed = deliberation.repository.load(workflow_id)
        if failed.status != "FAILED" or failed.initial_integration is not None:
            raise AssertionError("clone does not start at the saved failed checkpoint")
        saved_primary_calls = len(provider.calls)
        deliberation_state = asyncio.run(deliberation.recover(workflow_id))
        if deliberation_state.status != "COMPLETED":
            raise AssertionError(
                f"Deliberation clone did not complete: {deliberation_state.status}"
            )
        if PRIMARY_AGENT_IDS & set(provider.agent_calls):
            raise AssertionError("a saved primary analysis was sent to the Provider again")
        recovery_calls = provider.calls[saved_primary_calls:]
        if not recovery_calls or recovery_calls[0] != "InitialIntegratedAnalysis":
            raise AssertionError("recovery did not restart at Initial Integration")

        stakeholder_id = deliberation_state.analysis_results[
            "deliberation.stakeholder_response_analyst"
        ]["analysis_id"]
        for artifact_name in ("initial_integration", "final_integration"):
            artifact = getattr(deliberation_state, artifact_name)
            actual = artifact["stakeholder_structure"]["source_analysis_id"]
            if actual != stakeholder_id:
                raise AssertionError(
                    f"{artifact_name} has wrong stakeholder provenance: {actual}"
                )
        if not deliberation_state.deterministic_validation["passed"]:
            raise AssertionError("Deliberation deterministic validation did not pass")

        conclusion_waiting = asyncio.run(
            run_conclusion(conclusion, workflow_id=workflow_id)
        )
        if conclusion_waiting.status != "WAITING_HUMAN_SELECTION":
            raise AssertionError("Conclusion did not reach the Human Selection gate")
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

        result = {
            "workflow_id": workflow_id,
            "source_failed_stage": "initial_integration",
            "saved_primary_provider_calls_reexecuted": 0,
            "mock_recovery_calls": recovery_calls,
            "mock_provider_calls_total": len(provider.calls),
            "deliberation_status": deliberation_state.status,
            "deterministic_validation_passed": True,
            "stakeholder_source_analysis_id": stakeholder_id,
            "conclusion_gate_status": conclusion_waiting.status,
            "conclusion_status": conclusion_state.status,
            "playwright_status": playwright_state.status,
            "delivery_files": sorted(delivery_files),
            "accepted_gap_ids": sorted(accepted_gap_ids),
            "source_provider_reservations": source_provider_reservations,
            "source_retrieval_reservations": source_retrieval_reservations,
            "real_provider_calls": 0,
            "retrieval_calls": 0,
        }

    after_hashes = {
        str(path.relative_to(source_data)): sha256(path) for path in boundary
    }
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
