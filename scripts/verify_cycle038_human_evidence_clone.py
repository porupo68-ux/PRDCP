"""Read-only production clone verification for the Human Evidence Gate.

The source project is never opened for write. Only the Researcher workflow state and
its canonical report are copied into a temporary data directory.
"""

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

from providers.mock_provider import MockModelProvider
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.human_evidence import (
    HumanActorSource,
    HumanEvidenceDecisionType,
)
from storage.researcher_workflow_repository import ResearcherWorkflowRepository


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_if_present(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def verify(source_project: Path, workflow_id: str) -> dict:
    source_data = source_project / "storage" / "data"
    source_state = (
        source_data / "workflows" / "researcher" / f"{workflow_id}.json"
    )
    source_report = (
        source_data / "artifacts" / "research_reports" / f"{workflow_id}.json"
    )
    if not source_state.exists() or not source_report.exists():
        raise FileNotFoundError("Production Researcher state/report was not found")
    source_hashes = {
        "state": sha256(source_state),
        "report": sha256(source_report),
    }

    with tempfile.TemporaryDirectory() as temporary:
        clone_data = Path(temporary)
        clone_state = (
            clone_data / "workflows" / "researcher" / f"{workflow_id}.json"
        )
        clone_report = (
            clone_data / "artifacts" / "research_reports" / f"{workflow_id}.json"
        )
        copy_if_present(source_state, clone_state)
        copy_if_present(source_report, clone_report)
        provider = MockModelProvider(
            reservation_root=clone_data / "provider_call_reservations"
        )
        repository = ResearcherWorkflowRepository(clone_data)
        manager = ResearcherManager(
            ResearcherRegistry(provider, demo_safe_mode=True),
            repository,
            demo_safe_mode=True,
        )

        waiting = manager.recover_human_evidence_gate(workflow_id)
        summary = manager.inspect_human_evidence_gate(workflow_id)
        report = repository.load_report(workflow_id)
        repaired = [
            item
            for item in waiting.human_evidence_integrity_repairs
            if item.finding_id == "finding_qr_006"
        ]
        if waiting.status != "WAITING_HUMAN_EVIDENCE_REVIEW":
            raise AssertionError(f"unexpected clone status: {waiting.status}")
        if len(summary.evidence_sufficiency_findings) != 5:
            raise AssertionError("production clone must retain five evidence gaps")
        if summary.hard_integrity_findings or summary.unclassified_findings:
            raise AssertionError("production clone retained an unresolved integrity finding")
        if len(repaired) != 1:
            raise AssertionError("qf_006 was not repaired exactly once")
        repaired_source = next(
            item
            for item in report.sources
            if item.source_id == repaired[0].source_id
        )
        if repaired_source.source_type != "GOVERNMENT":
            raise AssertionError("qf_006 source was not reclassified to GOVERNMENT")
        if len(report.sources) != 13:
            raise AssertionError("deterministic repair changed the source count")

        completed = manager.decide_human_evidence(
            workflow_id,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS,
            reason="Production clone acceptance fault test only",
            actor_source=HumanActorSource.MOCK_FIXTURE,
        )
        outbox = repository.load_deliberation_outbox(workflow_id)
        recovered = manager.recover_human_evidence_gate(workflow_id)
        if completed.status != "COMPLETED" or recovered.status != "COMPLETED":
            raise AssertionError("clone did not complete idempotently")
        if outbox.message_id != repository.load_deliberation_outbox(workflow_id).message_id:
            raise AssertionError("clone recovery emitted a second handoff")
        if provider.calls:
            raise AssertionError("production clone verification made Provider calls")

    if source_hashes != {
        "state": sha256(source_state),
        "report": sha256(source_report),
    }:
        raise AssertionError("source workflow/report changed during clone verification")
    return {
        "workflow_id": workflow_id,
        "source_hashes": source_hashes,
        "source_count": 13,
        "resolved_integrity_finding_ids": ["finding_qr_006"],
        "accepted_evidence_finding_count": 5,
        "provider_calls": 0,
        "retrieval_calls": 0,
        "source_storage_mutated": False,
        "clone_status": "COMPLETED",
    }


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
