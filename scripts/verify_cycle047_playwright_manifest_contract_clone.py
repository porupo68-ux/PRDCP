"""Read-only production clone for Cycle 047 Citation Manifest repair."""

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


EXPECTED_PARAGRAPH_ID = "para_sec07_01"
EXPECTED_CLAIM_ID = "claim_03_phased_guideline_adaptation"
EXPECTED_EVIDENCE_ID = "evidence_mext_guideline_k12_2024"
EXPECTED_BLOCKING_CODES = [
    "CITATION_MAPPING_MISSING",
    "UNSUPPORTED_CLAIM_LIST_NOT_EMPTY",
]
EXPECTED_DELIVERY_FILES = {
    "citation_manifest.json",
    "final_script_package.json",
    "production_notes.md",
    "script.md",
    "source_list.md",
    "visual_plan.md",
}
PROTECTED_FIELDS = (
    "conclusion_handoff",
    "final_conclusion",
    "conclusion_package",
    "human_selection",
    "traceability_manifest",
    "production_context",
    "narrative_blueprint",
    "script_draft",
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

    with tempfile.TemporaryDirectory(prefix="prdcp-cycle047-clone-") as temporary:
        clone_data = Path(temporary)
        for source in boundary:
            copy_file(source, clone_data / source.relative_to(source_data))

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
            or blocked.deterministic_repair_count != 0
            or blocked.failed_agents
            or len(blocked.completed_agents) != 4
        ):
            raise AssertionError("clone does not start at the Cycle 047 boundary")
        error_findings = [
            item
            for item in blocked.deterministic_validation["findings"]
            if item["severity"] == "ERROR"
        ]
        if [item["code"] for item in error_findings] != EXPECTED_BLOCKING_CODES:
            raise AssertionError(f"unexpected blocking findings: {error_findings}")
        missing = error_findings[0]
        if missing["details"] != {
            "paragraph_id": EXPECTED_PARAGRAPH_ID,
            "claim_ids": [EXPECTED_CLAIM_ID],
            "evidence_ids": [EXPECTED_EVIDENCE_ID],
        }:
            raise AssertionError("saved missing mapping identity changed")
        unsupported_before = list(blocked.citation_manifest["unsupported_claims"])
        if len(unsupported_before) != 2:
            raise AssertionError("saved unsupported claim list changed")

        protected_before = {
            field: canonical_hash(getattr(blocked, field))
            for field in PROTECTED_FIELDS
        }
        state_path = playwright.repository.workflows_dir / f"{workflow_id}.json"
        source_state_hash = sha256(state_path)
        message_count_before = len(blocked.message_history)
        manifest_before = canonical_hash(blocked.citation_manifest)

        completed = asyncio.run(playwright.recover(workflow_id))
        if completed.status != "COMPLETED" or not completed.delivered:
            raise AssertionError("Playwright clone did not complete Delivery")
        if provider.calls or provider.agent_calls:
            raise AssertionError(f"deterministic repair called Provider: {provider.calls}")
        if completed.revision_count != 2:
            raise AssertionError("deterministic repair consumed LLM revision budget")
        if completed.deterministic_repair_count != 1:
            raise AssertionError("deterministic repair count is not one")
        if completed.final_gate_result["status"] != "APPROVED_WITH_LIMITATIONS":
            raise AssertionError("Final Gate did not preserve approved limitations")
        if completed.final_gate_result["blocking_finding_ids"]:
            raise AssertionError("Final Gate retained a blocking finding")
        if completed.citation_manifest["unsupported_claims"]:
            raise AssertionError("unsupported claims remain after repair")
        if completed.citation_validated_script["unresolved_citation_issues"]:
            raise AssertionError("stale validated-script citation issues remain")

        script_claim_ids = {
            claim_id
            for section in completed.script_draft["sections"]
            for paragraph in section["paragraphs"]
            for claim_id in paragraph["claim_ids"]
        }
        manifest_claim_ids = set(
            completed.citation_manifest["supported_claim_ids"]
        )
        if script_claim_ids != manifest_claim_ids or len(script_claim_ids) != 3:
            raise AssertionError("Script/Manifest claim contract is not exact")

        mappings_by_paragraph: dict[str, list[dict]] = {}
        for mapping in completed.citation_manifest["mappings"]:
            mappings_by_paragraph.setdefault(mapping["paragraph_id"], []).append(
                mapping
            )
        for section in completed.script_draft["sections"]:
            for paragraph in section["paragraphs"]:
                if not paragraph["citation_required"]:
                    continue
                mappings = mappings_by_paragraph.get(paragraph["paragraph_id"], [])
                mapped_claim_ids = {
                    value for mapping in mappings for value in mapping["claim_ids"]
                }
                mapped_evidence_ids = {
                    value
                    for mapping in mappings
                    for value in mapping["evidence_ids"]
                }
                if (
                    not mappings
                    or mapped_claim_ids != set(paragraph["claim_ids"])
                    or mapped_evidence_ids != set(paragraph["evidence_ids"])
                ):
                    raise AssertionError(
                        "citation-required paragraph contract is incomplete: "
                        + paragraph["paragraph_id"]
                    )

        protected_after = {
            field: canonical_hash(getattr(completed, field))
            for field in PROTECTED_FIELDS
        }
        if protected_after != protected_before:
            raise AssertionError("repair changed a protected upstream/content artifact")

        record = completed.deterministic_repair_history[0]
        if (
            record.repair_type
            != "CITATION_MANIFEST_CONTRACT_RECONSTRUCTION"
            or record.missing_mapping_ids != [EXPECTED_PARAGRAPH_ID]
            or set(record.unsupported_claim_ids)
            != {
                "claim_01_efficiency_and_debugging",
                "claim_02_skill_degradation_risk",
            }
            or record.script_claim_count != 3
            or record.manifest_claim_count_before != 0
            or record.manifest_claim_count_after != 3
            or record.unsupported_claim_count_before != 2
            or record.unsupported_claim_count_after != 0
            or record.citation_mapping_count_before != 11
            or record.citation_mapping_count_after != 12
            or record.provider_calls != 0
            or record.retrieval_calls != 0
        ):
            raise AssertionError("Cycle 047 repair audit is incomplete")
        if record.citation_manifest_hash_before != manifest_before:
            raise AssertionError("repair before hash does not match saved Manifest")
        if record.citation_manifest_hash_after != canonical_hash(
            completed.citation_manifest
        ):
            raise AssertionError("repair after hash does not match repaired Manifest")

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
            "source_state_hash": source_state_hash,
            "initial_status": blocked.status,
            "initial_revision_count": blocked.revision_count,
            "initial_blocking_codes": EXPECTED_BLOCKING_CODES,
            "script_claim_count": len(script_claim_ids),
            "manifest_claim_count": len(manifest_claim_ids),
            "unsupported_claim_count_before": len(unsupported_before),
            "unsupported_claim_count_after": len(
                completed.citation_manifest["unsupported_claims"]
            ),
            "citation_mapping_count_before": 11,
            "citation_mapping_count_after": len(
                completed.citation_manifest["mappings"]
            ),
            "repaired_paragraph_id": EXPECTED_PARAGRAPH_ID,
            "repair_id": record.repair_id,
            "manifest_hash_before": manifest_before,
            "manifest_hash_after": record.citation_manifest_hash_after,
            "protected_hashes_unchanged": True,
            "final_gate_status": completed.final_gate_result["status"],
            "blocking_finding_count": len(
                completed.final_gate_result["blocking_finding_ids"]
            ),
            "playwright_status": completed.status,
            "delivery_files": sorted(delivery_files),
            "delivery_message_count": len(delivery_messages),
            "saved_message_count_before": message_count_before,
            "saved_message_count_after": len(completed.message_history),
            "provider_reservations": provider_reservations,
            "retrieval_reservations": retrieval_reservations,
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
