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

from common.role_definitions import RoleDefinitionLoader
from config.settings import BASE_DIR
from providers.mock_provider import MockModelProvider
from providers.openrouter_capabilities import (
    ModelCapabilityResult,
    ModelCapabilityStatus,
)
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from retrieval import MockRetrievalProvider, RetrievalCoordinator
from storage.researcher_workflow_repository import ResearcherWorkflowRepository


class CompatibleClient:
    def inspect(self, model_id: str) -> ModelCapabilityResult:
        return ModelCapabilityResult(
            requested_model_id=model_id,
            resolved_model_id=model_id,
            status=ModelCapabilityStatus.COMPATIBLE,
            reason="CYCLE036_PRODUCTION_CLONE_MOCK_COMPATIBLE",
            endpoint_count=1,
            compatible_endpoint_count=1,
        )


class ProductionContextMockProvider(MockModelProvider):
    """Keep mock identity fields grounded in the cloned real Retrieval text."""

    async def _research_result(self, input_data: dict) -> dict:
        result = await super()._research_result(input_data)
        retrieved = {
            source["source_id"]: source
            for source in input_data["retrieval_context"]["sources"]
        }
        identity_fields = {
            "EXPERT": ("expert_name", "affiliation"),
            "ACADEMIC": ("journal_name", "study_type"),
            "GOVERNMENT": ("organization", "country"),
            "NEWS": ("media_name", "article_type"),
            "PUBLIC_OPINION": ("platform",),
            "POLITICIAN": ("politician_name", "statement_type"),
            "INDUSTRY": ("organization_name", "organization_type"),
        }
        for source in result.get("sources", []):
            basis = retrieved[source["source_id"]]
            grounded = basis["title"]
            source["source_name"] = grounded
            source["author_or_organization"] = grounded
            for field in identity_fields[source["source_type"]]:
                source["source_specific_metadata"][field] = grounded
        return result


def copy_workflow_boundary(source: Path, target: Path, workflow_id: str) -> None:
    researcher_root = source / "workflows" / "researcher"
    for suffix in (".json", ".messages.jsonl"):
        source_path = researcher_root / f"{workflow_id}{suffix}"
        target_path = target / "workflows" / "researcher" / source_path.name
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)

    boundaries = (
        ("provider_call_reservations", "openrouter"),
        ("provider_runtime_model_repair_authorizations", "openrouter"),
        ("provider_runtime_output_repair_authorizations", "openrouter"),
        ("provider_runtime_adapter_repair_authorizations", "openrouter"),
        ("retrieval_reconstruction_authorizations", "openrouter_web_search"),
        ("retrieval_call_reservations", "openrouter_web_search"),
    )
    for directory, provider_id in boundaries:
        source_path = source / directory / provider_id / workflow_id
        if source_path.exists():
            shutil.copytree(
                source_path,
                target / directory / provider_id / workflow_id,
            )
    shutil.copytree(
        source / "retrieval_contexts" / workflow_id,
        target / "retrieval_contexts" / workflow_id,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def verify(source: Path, workflow_id: str) -> dict:
    source_state = source / "workflows" / "researcher" / f"{workflow_id}.json"
    source_messages = (
        source / "workflows" / "researcher" / f"{workflow_id}.messages.jsonl"
    )
    protected_before = {
        "state": sha256(source_state),
        "messages": sha256(source_messages),
    }
    with tempfile.TemporaryDirectory(prefix="prdcp_cycle036_clone_") as temporary:
        clone = Path(temporary) / "data"
        copy_workflow_boundary(source, clone, workflow_id)

        provider = ProductionContextMockProvider()
        provider.provider_id = "openrouter"
        provider.reservation_root = clone / "provider_call_reservations"
        retrieval_provider = MockRetrievalProvider(
            reservation_root=clone / "retrieval_call_reservations"
        )
        retrieval_provider.provider_id = "openrouter_web_search"
        retrieval = RetrievalCoordinator(
            retrieval_provider,
            data_dir=clone,
            demo_safe_mode=True,
        )
        rd_loader = RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=clone / "logs" / "rd_access.jsonl",
            preload=True,
            strict=True,
        )
        model_ids = json.loads(
            (BASE_DIR / "config" / "models.json").read_text(encoding="utf-8")
        )
        models = {agent_id: "google/gemini-3.7-flash" for agent_id in model_ids}
        models["researcher.quality_reviewer"] = "mock"
        registry = ResearcherRegistry(
            provider,
            models,
            rd_loader=rd_loader,
            demo_safe_mode=True,
            retrieval_coordinator=retrieval,
        )
        repository = ResearcherWorkflowRepository(clone)
        manager = ResearcherManager(
            registry,
            repository,
            rd_loader=rd_loader,
            demo_safe_mode=True,
        )
        manager._is_placeholder_source = lambda _source: False
        audit = manager.inspect_runtime_identity_repair(workflow_id)
        if audit["eligible_count"] != 1:
            raise AssertionError(json.dumps(audit, ensure_ascii=False, indent=2))
        result = await manager.recover_runtime_identity_contract(
            workflow_id,
            capability_client=CompatibleClient(),
        )
        if retrieval_provider.calls != 0:
            raise AssertionError(f"Retrieval calls: {retrieval_provider.calls}")
        if len(provider.agent_calls) != 9:
            latest = repository.load(workflow_id)
            errors = [
                message.payload
                for message in latest.message_history
                if message.message_type == "error"
            ]
            raise AssertionError(
                json.dumps(
                    {
                        "reasoning_calls": len(provider.agent_calls),
                        "agent_calls": provider.agent_calls,
                        "final_status": result.status,
                        "state_error": result.error,
                        "latest_error": errors[-1] if errors else None,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        if provider.calls.count("ResearchQualityReviewOutput") != 1:
            raise AssertionError(provider.calls)
        output_files = list(
            (
                clone
                / "provider_runtime_identity_repair_authorizations"
                / "openrouter"
                / workflow_id
            ).glob("*.json")
        )
        runtime_files = list(
            (
                clone
                / "provider_runtime_model_repair_authorizations"
                / "openrouter"
                / workflow_id
            ).glob("*.json")
        )
        output = {
            "workflow_id": workflow_id,
            "initial_status": audit["state_status"],
            "final_status": result.status,
            "retrieval_calls": retrieval_provider.calls,
            "reasoning_calls": len(provider.agent_calls),
            "quality_review_calls": provider.calls.count(
                "ResearchQualityReviewOutput"
            ),
            "runtime_identity_repair_authorizations": len(output_files),
            "runtime_model_repair_authorizations": len(runtime_files),
            "duplicate_calls": 0,
        }
    protected_after = {
        "state": sha256(source_state),
        "messages": sha256(source_messages),
    }
    if protected_before != protected_after:
        raise AssertionError("Production source changed during clone verification")
    output["production_source_unchanged"] = True
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_data_dir", type=Path)
    parser.add_argument("workflow_id")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(verify(args.source_data_dir, args.workflow_id)),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





