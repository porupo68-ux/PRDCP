"""Read-only production clone verification for Cycle 046 coverage recovery."""

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
from conclusion.manager import CANDIDATE_COVERAGE_CONTRACT_REPAIR_SUFFIX
from conclusion.workflow import DECISION_EVALUATOR_ID
from providers.mock_provider import MockModelProvider
from runtime import build_all_managers


EXPECTED_CALLS = [
    "DecisionEvaluationResult",
    "DecisionIntegrationResult",
    "ConclusionQualityReviewOutput",
]


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
    if not boundary:
        raise FileNotFoundError("Production workflow boundary was not found")
    source_hashes = {
        str(path.relative_to(source_data)): sha256(path) for path in boundary
    }

    with tempfile.TemporaryDirectory(prefix="prdcp-cycle046-clone-") as temporary:
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
        _producer, _researcher, _deliberation, conclusion, _playwright = (
            build_all_managers(settings, provider=provider)
        )
        failed = conclusion.repository.load(workflow_id)
        if failed.status != "FAILED" or len(failed.position_candidates) != 3:
            raise AssertionError("clone is not at the saved Decision Evaluator boundary")
        if failed.decision_evaluation is not None:
            raise AssertionError("failed clone unexpectedly contains Decision Evaluation")
        saved_position = json.dumps(
            failed.position_generation,
            ensure_ascii=False,
            sort_keys=True,
        )
        failed_error = next(
            message
            for message in reversed(failed.message_history)
            if message.sender_agent_id == DECISION_EVALUATOR_ID
            and message.message_type == "error"
        )
        invalid = failed_error.payload.get("invalid_payload")
        if not isinstance(invalid, dict):
            raise AssertionError("saved failure has no auditable invalid payload")
        failed_evaluation_ids = {
            item.get("candidate_id")
            for item in invalid.get("candidate_evaluations", [])
            if isinstance(item, dict)
        }
        failed_matrix_ids = {
            item.get("candidate_id")
            for item in invalid.get("comparison_matrix", [])
            if isinstance(item, dict)
        }
        if len(failed_evaluation_ids) != 1 or len(failed_matrix_ids) != 3:
            raise AssertionError("clone does not reproduce the observed 1/3 coverage failure")

        recovered = asyncio.run(conclusion.recover(workflow_id))
        if recovered.status != "WAITING_HUMAN_SELECTION":
            raise AssertionError(f"unexpected recovered status: {recovered.status}")
        if provider.calls != EXPECTED_CALLS:
            raise AssertionError(f"unexpected clone Provider calls: {provider.calls}")
        if json.dumps(
            recovered.position_generation,
            ensure_ascii=False,
            sort_keys=True,
        ) != saved_position:
            raise AssertionError("saved Position Generator checkpoint was not reused")
        if not recovered.candidate_coverage_checked or not recovered.candidate_coverage_passed:
            raise AssertionError("recovered Candidate Coverage audit did not pass")
        audit = recovered.candidate_coverage_audit
        if (
            audit is None
            or audit.candidate_count_position_generator != 3
            or audit.candidate_count_evaluation != 3
            or audit.candidate_count_matrix != 3
            or audit.candidate_evaluation_row_count != 42
            or audit.missing_candidate_ids
            or audit.extra_candidate_ids
        ):
            raise AssertionError("recovered Candidate Coverage audit is inconsistent")
        evaluator_task_ids = [
            message.payload["task_id"]
            for message in recovered.message_history
            if message.sender_agent_id == "conclusion.manager"
            and message.receiver_agent_id == DECISION_EVALUATOR_ID
        ]
        if (
            len(evaluator_task_ids) != 2
            or len(set(evaluator_task_ids)) != 2
            or not evaluator_task_ids[-1].endswith(
                CANDIDATE_COVERAGE_CONTRACT_REPAIR_SUFFIX
            )
        ):
            raise AssertionError("coverage recovery did not use one new task identity")

        result = {
            "workflow_id": workflow_id,
            "initial_status": failed.status,
            "initial_candidate_count": len(failed.position_candidates),
            "failed_evaluation_candidate_count": len(failed_evaluation_ids),
            "failed_matrix_candidate_count": len(failed_matrix_ids),
            "recovered_status": recovered.status,
            "candidate_coverage_checked": recovered.candidate_coverage_checked,
            "candidate_coverage_passed": recovered.candidate_coverage_passed,
            "candidate_evaluation_row_count": audit.candidate_evaluation_row_count,
            "mock_provider_calls": list(provider.calls),
            "position_generator_reexecuted": False,
            "retrieval_calls": 0,
            "real_provider_calls": 0,
            "recovery_task_id": evaluator_task_ids[-1],
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
