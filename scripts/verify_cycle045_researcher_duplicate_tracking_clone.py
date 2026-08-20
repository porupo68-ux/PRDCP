"""Read-only production clone verification for Cycle 045 relation repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deliberation.manager import DeliberationManager
from deliberation.registry import DeliberationRegistry
from providers.mock_provider import MockModelProvider
from researcher.integrity_repair import immutable_report_sha256
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.human_evidence import (
    HumanActorSource,
    HumanEvidenceDecisionType,
)
from storage.deliberation_workflow_repository import DeliberationWorkflowRepository
from storage.researcher_workflow_repository import ResearcherWorkflowRepository


EXPECTED_HARD_FINDING = "finding-mext-guideline-duplicate-tracking"
EXPECTED_GAP_FINDINGS = {
    "finding-rq001-missing-industry",
    "finding-rq003-missing-news",
}
EXPECTED_CANONICAL_SOURCE = "source_494069a566daff953439ee54"
EXPECTED_CANONICAL_EVIDENCE = "evidence_mext_guideline_k12_2024"
EXPECTED_RELATED_SOURCES = {
    "source_5d706db6a0c176223171b43e",
    "source_c621935f8f9c45d0c50924da",
}
EXPECTED_MERGED_EVIDENCE = {
    "evidence_mext_guideline_page",
    "evidence_c621935f8f9c45d0c50924da_01",
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


def protected_sources(report) -> dict[str, dict]:
    protected: dict[str, dict] = {}
    for source in report.sources:
        payload = source.model_dump(mode="json")
        metadata = dict(payload.pop("source_specific_metadata"))
        metadata.pop("merged_evidence_ids", None)
        payload["source_specific_metadata"] = metadata
        protected[source.source_id] = payload
    return protected


def verify(source_project: Path, workflow_id: str) -> dict:
    source_data = source_project / "storage" / "data"
    boundary = workflow_boundary(source_data, workflow_id)
    if not boundary:
        raise FileNotFoundError("Production workflow boundary was not found")
    source_hashes = {
        str(path.relative_to(source_data)): sha256(path) for path in boundary
    }
    provider_reservations = sum(
        "provider_call_reservations" in path.parts for path in boundary
    )
    retrieval_reservations = sum(
        "retrieval_call_reservations" in path.parts for path in boundary
    )

    with tempfile.TemporaryDirectory(prefix="prdcp-cycle045-clone-") as temporary:
        clone_data = Path(temporary)
        for source in boundary:
            copy_file(source, clone_data / source.relative_to(source_data))

        provider = MockModelProvider(
            reservation_root=clone_data / "provider_call_reservations"
        )
        repository = ResearcherWorkflowRepository(clone_data)
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )
        before_state = repository.load(workflow_id)
        before_report = repository.load_report(workflow_id)
        before_review = json.loads(
            json.dumps(before_state.review_result, ensure_ascii=False, sort_keys=True)
        )
        before_protected = protected_sources(before_report)
        before_immutable = immutable_report_sha256(before_report)
        before_provider_reservations = sorted(
            str(path.relative_to(clone_data))
            for path in (clone_data / "provider_call_reservations").rglob("*.json")
        )
        before_retrieval_reservations = sorted(
            str(path.relative_to(clone_data))
            for path in (clone_data / "retrieval_call_reservations").rglob("*.json")
        )

        initial_summary = manager.inspect_human_evidence_gate(workflow_id)
        if before_state.status != "BLOCKED" or before_state.revision_count != 0:
            raise AssertionError("production clone is not at the Cycle 045 boundary")
        if len(before_report.sources) != 21:
            raise AssertionError("production clone does not contain 21 saved Sources")
        if {item.finding_id for item in initial_summary.hard_integrity_findings} != {
            EXPECTED_HARD_FINDING
        }:
            raise AssertionError("unexpected pre-repair Hard Integrity Finding")
        if {
            item.finding_id for item in initial_summary.evidence_sufficiency_findings
        } != EXPECTED_GAP_FINDINGS:
            raise AssertionError("unexpected pre-repair Evidence Sufficiency Findings")

        waiting = manager.repair_human_evidence_integrity(workflow_id)
        repaired_report = repository.load_report(workflow_id)
        summary = manager.inspect_human_evidence_gate(workflow_id)
        if waiting.status != "WAITING_HUMAN_EVIDENCE_REVIEW":
            raise AssertionError(f"unexpected repaired status: {waiting.status}")
        if waiting.revision_count != 0 or len(repaired_report.sources) != 21:
            raise AssertionError("relation repair changed revision budget or Source count")
        if summary.hard_integrity_findings or summary.unclassified_findings:
            raise AssertionError("relation repair retained a blocking integrity finding")
        if {
            item.finding_id for item in summary.evidence_sufficiency_findings
        } != EXPECTED_GAP_FINDINGS:
            raise AssertionError("relation repair changed the two Evidence gaps")
        if {item.finding_id for item in summary.resolved_integrity_findings} != {
            EXPECTED_HARD_FINDING
        }:
            raise AssertionError("duplicate tracking finding was not resolved exactly once")
        if protected_sources(repaired_report) != before_protected:
            raise AssertionError("Source identity, content, URL or RQ assignment changed")
        if immutable_report_sha256(repaired_report) != before_immutable:
            raise AssertionError("protected Research Report content changed")
        if waiting.review_result != before_review:
            raise AssertionError("saved Quality Review changed")

        canonical = next(
            source
            for source in repaired_report.sources
            if source.source_id == EXPECTED_CANONICAL_SOURCE
        )
        if canonical.evidence_id != EXPECTED_CANONICAL_EVIDENCE:
            raise AssertionError("canonical Source/Evidence identity changed")
        if set(
            canonical.source_specific_metadata.get("merged_evidence_ids") or []
        ) != EXPECTED_MERGED_EVIDENCE:
            raise AssertionError("canonical same-document relation was not persisted")
        repair = waiting.human_evidence_integrity_repairs[-1]
        if (
            repair.repair_kind != "research_source_duplicate_tracking"
            or repair.canonical_source_id != EXPECTED_CANONICAL_SOURCE
            or set(repair.related_source_ids) != EXPECTED_RELATED_SOURCES
            or set(repair.merged_evidence_ids) != EXPECTED_MERGED_EVIDENCE
            or repair.provider_calls != 0
            or repair.retrieval_calls != 0
        ):
            raise AssertionError("repair audit artifact does not match the relation change")
        artifact_paths = list(
            (
                repository.human_evidence_integrity_repairs_dir / workflow_id
            ).glob("*.json")
        )
        if len(artifact_paths) != 1:
            raise AssertionError("repair audit artifact was not persisted exactly once")
        if provider.calls or provider.agent_calls:
            raise AssertionError("deterministic clone repair called the Provider")
        if before_provider_reservations != sorted(
            str(path.relative_to(clone_data))
            for path in (clone_data / "provider_call_reservations").rglob("*.json")
        ):
            raise AssertionError("clone repair changed Provider reservations")
        if before_retrieval_reservations != sorted(
            str(path.relative_to(clone_data))
            for path in (clone_data / "retrieval_call_reservations").rglob("*.json")
        ):
            raise AssertionError("clone repair changed Retrieval reservations")

        state_path = repository.workflows_dir / f"{workflow_id}.json"
        report_path = repository.reports_dir / f"{workflow_id}.json"
        repaired_state_hash = sha256(state_path)
        repaired_report_hash = sha256(report_path)
        repeated = manager.repair_human_evidence_integrity(workflow_id)
        if (
            repeated.status != "WAITING_HUMAN_EVIDENCE_REVIEW"
            or sha256(state_path) != repaired_state_hash
            or sha256(report_path) != repaired_report_hash
            or len(
                list(
                    (
                        repository.human_evidence_integrity_repairs_dir / workflow_id
                    ).glob("*.json")
                )
            )
            != 1
        ):
            raise AssertionError("repeated relation repair was not an exact no-op")

        completed = manager.decide_human_evidence(
            workflow_id,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS,
            reason="Cycle 045 clone-only downstream contract verification",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )
        if completed.status != "COMPLETED" or len(completed.accepted_evidence_gaps) != 2:
            raise AssertionError("clone Human Decision boundary did not preserve both gaps")
        handoff = repository.load_deliberation_outbox(workflow_id)
        deliberation = DeliberationManager(
            DeliberationRegistry(provider, demo_safe_mode=True),
            DeliberationWorkflowRepository(clone_data),
            demo_safe_mode=True,
        )
        downstream_report = deliberation._validate_researcher_handoff(handoff)
        context = deliberation._research_context_from_state(
            type(
                "StateView",
                (),
                {"researcher_handoff": handoff.model_dump(mode="json")},
            )(),
            downstream_report,
        )
        if [
            item.repair_kind for item in context.human_evidence_integrity_repairs
        ] != ["research_source_duplicate_tracking"]:
            raise AssertionError("Deliberation rejected or lost the new repair contract")

        result = {
            "workflow_id": workflow_id,
            "initial_status": before_state.status,
            "initial_source_count": len(before_report.sources),
            "initial_revision_count": before_state.revision_count,
            "resolved_integrity_finding_ids": [EXPECTED_HARD_FINDING],
            "remaining_evidence_finding_ids": sorted(EXPECTED_GAP_FINDINGS),
            "canonical_source_id": repair.canonical_source_id,
            "document_family_id": repair.document_family_id,
            "related_source_ids": sorted(repair.related_source_ids),
            "merged_evidence_ids": sorted(repair.merged_evidence_ids),
            "source_identity_and_content_unchanged": True,
            "quality_review_unchanged": True,
            "clone_gate_status": waiting.status,
            "clone_downstream_status": completed.status,
            "deliberation_contract_validated": True,
            "source_provider_reservations": provider_reservations,
            "source_retrieval_reservations": retrieval_reservations,
            "provider_calls": 0,
            "retrieval_calls": 0,
            "repeat_repair_writes": 0,
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
