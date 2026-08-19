"""Read-only production-state clone for Cycle 044 citation mapping repair."""

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
from playwright.validator import canonical_hash
from providers.mock_provider import MockModelProvider
from runtime import build_all_managers
from storage.researcher_workflow_repository import ResearcherWorkflowRepository


EXPECTED_PARAGRAPH_ID = "para_005_analysis_03"
EXPECTED_CLAIM_ID = "claim_genai_macro_labor_shortage_cushion_004"
EXPECTED_EVIDENCE_ID = "evidence_cao_world_economy_2024"
EXPECTED_ACCEPTED_GAP = "fqrq_ai_emp_003_news_missing"
EXPECTED_DELIVERY_FILES = {
    "citation_manifest.json",
    "final_script_package.json",
    "production_notes.md",
    "script.md",
    "source_list.md",
    "visual_plan.md",
}
PROTECTED_FIELDS = (
    "final_conclusion",
    "production_context",
    "narrative_blueprint",
    "script_draft",
    "citation_validated_script",
    "visual_plan",
)


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
    provider_reservations = sum(
        "provider_call_reservations" in path.parts for path in boundary
    )
    retrieval_reservations = sum(
        "retrieval_call_reservations" in path.parts for path in boundary
    )

    with tempfile.TemporaryDirectory(prefix="prdcp-cycle044-clone-") as temporary:
        clone_data = Path(temporary)
        for source in boundary:
            copy_file(source, clone_data / source.relative_to(source_data))

        researcher_state = ResearcherWorkflowRepository(clone_data).load(workflow_id)
        accepted_gap_ids = {
            item.finding_id for item in researcher_state.accepted_evidence_gaps
        }
        if researcher_state.status != "COMPLETED" or len(
            researcher_state.collected_sources
        ) != 27:
            raise AssertionError("saved Researcher boundary changed")
        if EXPECTED_ACCEPTED_GAP not in accepted_gap_ids:
            raise AssertionError("accepted unresolved NEWS gap is missing")

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
        _producer, _researcher, _deliberation, _conclusion, playwright = (
            build_all_managers(settings, provider=provider)
        )
        blocked = playwright.repository.load(workflow_id)
        if (
            blocked.status != "BLOCKED"
            or blocked.revision_count != 2
            or blocked.failed_agents
            or len(blocked.completed_agents) != 4
        ):
            raise AssertionError("clone does not start at the Cycle 044 boundary")
        error_findings = [
            item
            for item in blocked.deterministic_validation["findings"]
            if item["severity"] == "ERROR"
        ]
        if [item["code"] for item in error_findings] != [
            "CITATION_MAPPING_MISSING"
        ]:
            raise AssertionError(f"unexpected blocking findings: {error_findings}")
        if error_findings[0]["details"] != {
            "paragraph_id": EXPECTED_PARAGRAPH_ID,
            "claim_ids": [EXPECTED_CLAIM_ID],
            "evidence_ids": [EXPECTED_EVIDENCE_ID],
        }:
            raise AssertionError("saved citation finding identity changed")

        protected_before = {
            field: canonical_hash(getattr(blocked, field))
            for field in PROTECTED_FIELDS
        }
        manifest_before = canonical_hash(blocked.citation_manifest)
        message_count_before = len(blocked.message_history)

        completed = asyncio.run(playwright.recover(workflow_id))
        if completed.status != "COMPLETED" or not completed.delivered:
            raise AssertionError("Playwright clone did not complete Delivery")
        if provider.calls or provider.agent_calls:
            raise AssertionError(f"deterministic repair called Provider: {provider.calls}")
        if completed.revision_count != 2:
            raise AssertionError("deterministic repair consumed LLM revision budget")
        if completed.deterministic_repair_count != 1:
            raise AssertionError("deterministic repair count is not one")
        if completed.final_gate_result["blocking_finding_ids"]:
            raise AssertionError("Final Gate retained a blocking finding")
        remaining_codes = {
            item["code"] for item in completed.final_gate_result["findings"]
        }
        if remaining_codes - {"SCRIPT_CHARACTER_COUNT_MISMATCH"}:
            raise AssertionError(f"unexpected post-repair findings: {remaining_codes}")
        protected_after = {
            field: canonical_hash(getattr(completed, field))
            for field in PROTECTED_FIELDS
        }
        if protected_after != protected_before:
            raise AssertionError("deterministic repair changed protected content")

        target_mappings = [
            mapping
            for mapping in completed.citation_manifest["mappings"]
            if mapping["paragraph_id"] == EXPECTED_PARAGRAPH_ID
        ]
        if len(target_mappings) != 1:
            raise AssertionError("target citation mapping was not reconstructed once")
        target = target_mappings[0]
        if (
            target["claim_ids"] != [EXPECTED_CLAIM_ID]
            or target["evidence_ids"] != [EXPECTED_EVIDENCE_ID]
            or len(target["source_ids"]) != 1
        ):
            raise AssertionError("reconstructed mapping lost canonical traceability")

        record = completed.deterministic_repair_history[0]
        manifest_after = canonical_hash(completed.citation_manifest)
        if (
            record.citation_manifest_hash_before != manifest_before
            or record.citation_manifest_hash_after != manifest_after
            or record.provider_calls != 0
            or record.retrieval_calls != 0
        ):
            raise AssertionError("repair audit artifact does not match the mutation")
        repair_files = list(
            (
                playwright.repository.deterministic_repair_dir / workflow_id
            ).glob("*.json")
        )
        if len(repair_files) != 1:
            raise AssertionError("repair artifact was not persisted exactly once")

        delivery_dir = clone_data / "deliveries" / workflow_id
        delivery_files = {
            path.name for path in delivery_dir.iterdir() if path.is_file()
        }
        if delivery_files != EXPECTED_DELIVERY_FILES:
            raise AssertionError(f"unexpected Delivery files: {delivery_files}")
        delivery_messages = [
            message
            for message in completed.message_history
            if message.message_type == "final_script_delivery"
        ]
        if len(delivery_messages) != 1:
            raise AssertionError("Delivery PMP message is not exactly once")

        state_path = (
            playwright.repository.workflows_dir / f"{workflow_id}.json"
        )
        completed_state_hash = sha256(state_path)
        repeated = asyncio.run(playwright.recover(workflow_id))
        if (
            repeated.status != "COMPLETED"
            or sha256(state_path) != completed_state_hash
            or len(repeated.message_history) != len(completed.message_history)
            or len(
                list(
                    (
                        playwright.repository.deterministic_repair_dir
                        / workflow_id
                    ).glob("*.json")
                )
            )
            != 1
        ):
            raise AssertionError("repeated recovery was not an idempotent no-op")

        result = {
            "workflow_id": workflow_id,
            "initial_playwright_status": blocked.status,
            "initial_revision_count": blocked.revision_count,
            "initial_blocking_codes": [item["code"] for item in error_findings],
            "repaired_paragraph_id": EXPECTED_PARAGRAPH_ID,
            "mapping_id": target["citation_mapping_id"],
            "repair_id": record.repair_id,
            "manifest_hash_before": manifest_before,
            "manifest_hash_after": manifest_after,
            "protected_hashes_unchanged": True,
            "final_gate_status": completed.final_gate_result["status"],
            "remaining_finding_codes": sorted(remaining_codes),
            "playwright_status": completed.status,
            "delivery_files": sorted(delivery_files),
            "delivery_message_count": len(delivery_messages),
            "saved_message_count_before": message_count_before,
            "saved_message_count_after": len(completed.message_history),
            "deterministic_repair_count": completed.deterministic_repair_count,
            "llm_revision_count_after": completed.revision_count,
            "accepted_gap_ids": sorted(accepted_gap_ids),
            "researcher_source_count": len(researcher_state.collected_sources),
            "source_provider_reservations": provider_reservations,
            "source_retrieval_reservations": retrieval_reservations,
            "provider_calls": 0,
            "retrieval_calls": 0,
            "repeat_recover_writes": 0,
        }

    after_hashes = {
        str(path.relative_to(source_data)): sha256(path) for path in boundary
    }
    if source_hashes != after_hashes:
        raise AssertionError("production source boundary changed during clone verification")
    result["source_storage_mutated"] = False
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
