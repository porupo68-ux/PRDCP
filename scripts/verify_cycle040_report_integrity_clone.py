"""Read-only production clone verification for Research Report integrity repair."""

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
from storage.researcher_workflow_repository import ResearcherWorkflowRepository

TARGET_SOURCE_ID = "source_134a53bf88dcde074747e0a5"
EXPECTED_INITIAL_EVIDENCE_SHA256 = (
    "3c1a32f3608bb7d5289d01e6fce1a71cc0dd2403ea8af5d0ca8f0f952bf18cb5"
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def verify(source_project: Path, workflow_id: str) -> dict:
    source_data = source_project / "storage" / "data"
    source_state = source_data / "workflows" / "researcher" / f"{workflow_id}.json"
    source_report = (
        source_data / "artifacts" / "research_reports" / f"{workflow_id}.json"
    )
    if not source_state.exists() or not source_report.exists():
        raise FileNotFoundError("Production Researcher state/report was not found")
    source_hashes = {"state": sha256(source_state), "report": sha256(source_report)}

    with tempfile.TemporaryDirectory(prefix="prdcp-cycle040-clone-") as temporary:
        clone_data = Path(temporary)
        clone_state = clone_data / "workflows" / "researcher" / source_state.name
        clone_report = clone_data / "artifacts" / "research_reports" / source_report.name
        copy_file(source_state, clone_state)
        copy_file(source_report, clone_report)
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
        before_source = next(
            item for item in before_report.sources if item.source_id == TARGET_SOURCE_ID
        ).model_dump(mode="json")
        before_source_limitations = {
            item.source_id: list(item.limitations) for item in before_report.sources
        }
        provider_reservations_before = list(
            (clone_data / "provider_call_reservations").rglob("*.json")
        )
        retrieval_reservations_before = list(
            (clone_data / "retrieval_call_reservations").rglob("*.json")
        )

        waiting = manager.recover_human_evidence_gate(workflow_id)
        summary = manager.inspect_human_evidence_gate(workflow_id)
        after_report = repository.load_report(workflow_id)
        after_source = next(
            item for item in after_report.sources if item.source_id == TARGET_SOURCE_ID
        ).model_dump(mode="json")
        repairs = waiting.human_evidence_integrity_repairs

        if waiting.status != "WAITING_HUMAN_EVIDENCE_REVIEW":
            raise AssertionError(f"unexpected clone status: {waiting.status}")
        if summary.hard_integrity_findings or summary.unclassified_findings:
            raise AssertionError("production clone retained an unresolved integrity finding")
        if [item.finding_id for item in summary.evidence_sufficiency_findings] != [
            "fqrq_ai_emp_003_news_missing"
        ]:
            raise AssertionError("the unrelated rq_ai_emp_003 NEWS gap was lost")
        if len(after_report.sources) != 27 or len(before_report.sources) != 27:
            raise AssertionError("deterministic repair changed the 27-source evidence set")
        if len(before_report.research_limitations) != 54:
            raise AssertionError("unexpected pre-repair Report limitation count")
        if len(after_report.research_limitations) != 14:
            raise AssertionError("exact duplicate repair did not retain 14 global limitations")
        if before_source_limitations != {
            item.source_id: list(item.limitations) for item in after_report.sources
        }:
            raise AssertionError("source-level limitations changed")
        if after_source["source_type"] != "NEWS":
            raise AssertionError("recognized media source was not reclassified to NEWS")
        if after_source["research_question_ids"] != ["rq_ai_emp_002"]:
            raise AssertionError("source/question traceability changed")
        immutable_fields = {
            "source_id",
            "evidence_id",
            "research_question_ids",
            "title",
            "source_name",
            "url",
            "author_or_organization",
            "published_at",
            "retrieved_at",
            "summary",
            "relevant_excerpt",
            "stance",
            "reliability",
            "directness",
            "primary_source",
            "geographic_scope",
            "time_scope",
            "limitations",
        }
        if any(before_source[field] != after_source[field] for field in immutable_fields):
            raise AssertionError("classification repair changed source identity or content")
        if waiting.review_result != before_review:
            raise AssertionError("original Quality Review changed")
        if len(repairs) != 2:
            raise AssertionError("expected exactly two deterministic repair artifacts")
        if repairs[0].evidence_set_sha256_before != EXPECTED_INITIAL_EVIDENCE_SHA256:
            raise AssertionError("initial Evidence Set identity changed")
        if repairs[0].evidence_set_sha256_after != repairs[1].evidence_set_sha256_before:
            raise AssertionError("repair Evidence Set hash chain is discontinuous")
        if repairs[1].evidence_set_sha256_after != summary.evidence_set_sha256:
            raise AssertionError("Human Gate is not bound to the repaired Evidence Set")
        if any(item.provider_calls or item.retrieval_calls for item in repairs):
            raise AssertionError("repair artifact recorded an external call")
        if provider.calls:
            raise AssertionError("clone recovery made Provider calls")
        if provider_reservations_before != list(
            (clone_data / "provider_call_reservations").rglob("*.json")
        ):
            raise AssertionError("clone recovery changed Provider reservations")
        if retrieval_reservations_before != list(
            (clone_data / "retrieval_call_reservations").rglob("*.json")
        ):
            raise AssertionError("clone recovery changed Retrieval reservations")

    if source_hashes != {"state": sha256(source_state), "report": sha256(source_report)}:
        raise AssertionError("source workflow/report changed during clone verification")
    return {
        "workflow_id": workflow_id,
        "source_hashes": source_hashes,
        "source_count_before": 27,
        "source_count_after": 27,
        "report_limitations_before": 54,
        "report_limitations_after": 14,
        "resolved_integrity_finding_ids": [
            "fqr_limitations_duplication",
            "fqr_source_classification_expert_news",
        ],
        "remaining_evidence_finding_ids": ["fqrq_ai_emp_003_news_missing"],
        "provider_calls": 0,
        "retrieval_calls": 0,
        "source_storage_mutated": False,
        "clone_status": "WAITING_HUMAN_EVIDENCE_REVIEW",
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
