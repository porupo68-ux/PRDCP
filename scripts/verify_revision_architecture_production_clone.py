"""Read-only production clone check for the shared Revision Architecture.

The source workflow boundary is hashed before and after the check.  Every file
whose path contains the workflow ID is copied to an isolated temporary data
directory, then loaded by the current five-layer runtime with Mock providers.
No source artifact is rewritten and no real Provider or Retrieval call is made.
"""

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

from common.models import RevisionControlPhase
from config.settings import Settings
from providers.mock_provider import MockModelProvider
from runtime import build_all_managers


EXPECTED_DELIVERY_FILES = {
    "citation_manifest.json",
    "final_script_package.json",
    "production_notes.md",
    "script.md",
    "source_list.md",
    "visual_plan.md",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workflow_boundary(source_data: Path, workflow_id: str) -> list[Path]:
    return sorted(
        path
        for path in source_data.rglob("*")
        if path.is_file() and workflow_id in str(path.relative_to(source_data))
    )


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def verify(source_project: Path, workflow_id: str) -> dict[str, object]:
    source_data = source_project / "storage" / "data"
    boundary = _workflow_boundary(source_data, workflow_id)
    if not boundary:
        raise FileNotFoundError(f"No saved boundary exists for workflow {workflow_id}")
    source_hashes = {
        str(path.relative_to(source_data)): _sha256(path) for path in boundary
    }

    workflow_files = {
        layer: source_data / "workflows" / layer / f"{workflow_id}.json"
        for layer in (
            "producer",
            "researcher",
            "deliberation",
            "conclusion",
            "playwright",
        )
    }
    missing = [layer for layer, path in workflow_files.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Saved five-layer workflow is incomplete: {missing}")
    legacy_without_revision_control = [
        layer
        for layer, path in workflow_files.items()
        if "revision_control" not in json.loads(path.read_text(encoding="utf-8"))
    ]

    with tempfile.TemporaryDirectory(prefix="prdcp-revision-clone-") as temporary:
        clone_data = Path(temporary)
        for source in boundary:
            _copy(source, clone_data / source.relative_to(source_data))

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
        producer, researcher, deliberation, conclusion, playwright = (
            build_all_managers(settings, provider=provider)
        )
        managers = {
            "producer": producer,
            "researcher": researcher,
            "deliberation": deliberation,
            "conclusion": conclusion,
            "playwright": playwright,
        }
        loaded = {
            layer: manager.repository.load(workflow_id)
            for layer, manager in managers.items()
        }
        statuses_before = {
            layer: state.status for layer, state in loaded.items()
        }
        for layer, state in loaded.items():
            if state.workflow_id != workflow_id:
                raise AssertionError(f"{layer} workflow identity changed on load")
            if state.revision_control.phase != RevisionControlPhase.IDLE.value:
                raise AssertionError(
                    f"{layer} legacy state did not receive an IDLE revision default"
                )

        clone_boundary_before = _workflow_boundary(clone_data, workflow_id)
        clone_hashes_before = {
            str(path.relative_to(clone_data)): _sha256(path)
            for path in clone_boundary_before
        }
        restarted = {
            "producer": asyncio.run(producer.recover(workflow_id)),
            "researcher": asyncio.run(researcher.start(workflow_id)),
            "deliberation": asyncio.run(deliberation.start(workflow_id)),
            "conclusion": asyncio.run(conclusion.start(workflow_id)),
            "playwright": asyncio.run(playwright.start(workflow_id)),
        }
        statuses_after = {
            layer: state.status for layer, state in restarted.items()
        }
        if statuses_after != statuses_before:
            raise AssertionError(
                f"completed clone state changed during idempotent restart: "
                f"before={statuses_before}, after={statuses_after}"
            )
        if provider.calls or provider.agent_calls:
            raise AssertionError(f"production clone made Provider calls: {provider.calls}")

        clone_boundary_after = _workflow_boundary(clone_data, workflow_id)
        clone_hashes_after = {
            str(path.relative_to(clone_data)): _sha256(path)
            for path in clone_boundary_after
        }
        if clone_hashes_after != clone_hashes_before:
            raise AssertionError("idempotent clone restart rewrote a saved workflow artifact")

        delivery_dir = clone_data / "deliveries" / workflow_id
        delivery_files = {
            path.name for path in delivery_dir.iterdir() if path.is_file()
        }
        if delivery_files != EXPECTED_DELIVERY_FILES:
            raise AssertionError(f"unexpected Delivery files: {delivery_files}")
        delivery_messages = [
            message
            for message in restarted["playwright"].message_history
            if message.message_type == "final_script_delivery"
        ]
        if len(delivery_messages) != 1:
            raise AssertionError("completed clone must retain exactly one Delivery message")

        result: dict[str, object] = {
            "workflow_id": workflow_id,
            "source_boundary_files": len(boundary),
            "legacy_states_without_revision_control": legacy_without_revision_control,
            "revision_default": RevisionControlPhase.IDLE.value,
            "statuses": statuses_after,
            "delivery_files": sorted(delivery_files),
            "delivery_message_count": len(delivery_messages),
            "provider_calls": 0,
            "retrieval_calls": 0,
            "clone_restart_writes": 0,
        }

    after_hashes = {
        str(path.relative_to(source_data)): _sha256(path) for path in boundary
    }
    if after_hashes != source_hashes:
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
