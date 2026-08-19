from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config.settings as settings_module
from cli_app.diagnostics import run_doctor
from common.provider_runtime_model_repair import RuntimeModelRepairStatus
from common.provider_runtime_model_repair import RUNTIME_MODEL_REPAIR_SUFFIX
from common.runtime_models import RuntimeModelGuard, audit_runtime_models
from config.settings import Settings, load_env_file
from producer.schemas.research_plan import ResearchPlan
from providers.mock_provider import MockModelProvider
from providers.openrouter_capabilities import (
    ModelCapabilityResult,
    ModelCapabilityStatus,
)
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from tests.researcher_helpers import make_handoff, make_plan
from runtime import build_all_managers
from retrieval import RetrievalCoordinator


OLD_MODEL = "perplexity/sonar-deep-research"
CURRENT_MODEL = "google/gemini-3.7-flash"
AGENT_ID = "researcher.academic_researcher"
GOVERNMENT_AGENT_ID = "researcher.government_researcher"


class CompatibleClient:
    def inspect(self, model_id: str) -> ModelCapabilityResult:
        return ModelCapabilityResult(
            requested_model_id=model_id,
            resolved_model_id=model_id,
            status=ModelCapabilityStatus.COMPATIBLE,
            reason="TEST_COMPATIBLE",
            endpoint_count=1,
            compatible_endpoint_count=1,
        )


class Cycle032RuntimeModelRecoveryTests(unittest.TestCase):
    @staticmethod
    def settings(data_dir: Path) -> Settings:
        model_ids = json.loads(
            (settings_module.BASE_DIR / "config" / "models.json").read_text(
                encoding="utf-8"
            )
        )
        return Settings(
            provider="mock",
            discord_bot_token=None,
            openrouter_api_key=None,
            openrouter_base_url="https://openrouter.ai/api/v1",
            data_dir=data_dir,
            log_level="INFO",
            models={agent_id: "mock" for agent_id in model_ids},
            retrieval_provider="mock",
            demo_safe_mode=True,
        )

    @staticmethod
    def plan_for_targets(*targets: str) -> ResearchPlan:
        payload = make_plan().model_dump(mode="json")
        payload["research_questions"] = [
            {
                **payload["research_questions"][0],
                "research_targets": list(targets),
            }
        ]
        return ResearchPlan.model_validate(payload)

    def failed_workflow(self, root: Path, *targets: str):
        agent_ids = {
            "ACADEMIC": AGENT_ID,
            "GOVERNMENT": GOVERNMENT_AGENT_ID,
        }
        failed_agent_ids = {agent_ids[target] for target in targets}
        repository = ResearcherWorkflowRepository(root)
        provider = MockModelProvider(fail_agent_ids=failed_agent_ids)
        provider.reservation_root = root / "provider_call_reservations"
        registry = ResearcherRegistry(
            provider,
            {agent_id: OLD_MODEL for agent_id in failed_agent_ids},
            demo_safe_mode=True,
        )
        manager = ResearcherManager(
            registry,
            repository,
            demo_safe_mode=True,
        )
        state = asyncio.run(
            manager.start_from_message(make_handoff(self.plan_for_targets(*targets)))
        )
        self.assertEqual(state.status, "FAILED")
        for error_index, old_error in enumerate(state.message_history):
            if old_error.message_type != "error":
                continue
            state.message_history[error_index] = old_error.model_copy(
                update={
                    "payload": {
                        **old_error.payload,
                        "error_code": "ProviderCapabilityError",
                        "error_class": "ProviderCapabilityError",
                        "http_status": 404,
                        "model_id": OLD_MODEL,
                        "automatic_retry_allowed": False,
                    }
                }
            )
        repository.save(state)
        return state, repository, provider

    def failed_one_task_workflow(self, root: Path):
        return self.failed_workflow(root, "ACADEMIC")

    def repaired_manager(self, repository, provider):
        provider.fail_agent_ids.clear()
        provider.calls.clear()
        provider.agent_calls.clear()
        registry = ResearcherRegistry(
            provider,
            {
                AGENT_ID: CURRENT_MODEL,
                "researcher.quality_reviewer": "mock",
            },
            demo_safe_mode=True,
        )
        manager = ResearcherManager(
            registry,
            repository,
            demo_safe_mode=True,
        )
        retrieval_provider = registry.get(AGENT_ID).retrieval_coordinator.provider
        return manager, retrieval_provider

    def test_dotenv_refresh_updates_only_loader_owned_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            key = "CYCLE032_MODEL_TEST"
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(key, None)
                settings_module._DOTENV_MANAGED_VALUES.pop(key, None)
                path.write_text(f"{key}=old/model\n", encoding="utf-8")
                load_env_file(path)
                self.assertEqual(os.environ[key], "old/model")
                path.write_text(f"{key}=new/model\n", encoding="utf-8")
                load_env_file(path, refresh=True)
                self.assertEqual(os.environ[key], "new/model")
                os.environ[key] = "operator/model"
                path.write_text(f"{key}=ignored/model\n", encoding="utf-8")
                load_env_file(path, refresh=True)
                self.assertEqual(os.environ[key], "operator/model")
                os.environ.pop(key, None)
                settings_module._DOTENV_MANAGED_VALUES.pop(key, None)

    def test_runtime_repair_identity_keeps_original_retrieval_identity(self) -> None:
        original = "task_cycle032"
        self.assertEqual(
            RetrievalCoordinator._canonical_task_id(
                original + RUNTIME_MODEL_REPAIR_SUFFIX
            ),
            original,
        )

    def test_all_31_runtime_models_are_audited_and_drift_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            managers = build_all_managers(settings)
            audit = audit_runtime_models(settings, managers)
            self.assertEqual(len(audit.entries), 31)
            self.assertEqual(audit.drifted, ())

            researcher = managers[1]
            researcher.registry.get(AGENT_ID).model = OLD_MODEL
            drifted = audit_runtime_models(settings, managers)
            self.assertEqual([item.agent_id for item in drifted.drifted], [AGENT_ID])
            guard = RuntimeModelGuard(managers, settings_loader=lambda: settings)
            provider = researcher.registry.get(AGENT_ID).provider
            before = len(provider.calls)
            with self.assertRaisesRegex(RuntimeError, "RUNTIME_MODEL_DRIFT"):
                guard.require_current(layer="researcher", operation="test")
            self.assertEqual(len(provider.calls), before)
            reservation_root = settings.data_dir / "provider_call_reservations"
            self.assertEqual(list(reservation_root.rglob("*.json")), [])

            checks = run_doctor(settings, runtime_managers=managers)
            runtime_check = next(
                item for item in checks if item.name == "RUNTIME MODEL DRIFT"
            )
            self.assertEqual(runtime_check.level, "FAIL")

    def test_verified_binding_is_reported_without_false_runtime_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = self.settings(Path(temporary))
            managers = build_all_managers(settings)
            binding = SimpleNamespace(
                agent_id="conclusion.position_generator",
                incompatible_model_id="mock",
                compatible_model_id="verified/replacement",
            )
            with patch(
                "common.runtime_models.ProviderModelCompatibilityStore.list_verified",
                return_value=[binding],
            ):
                audit = audit_runtime_models(settings, managers)
            entry = next(
                item
                for item in audit.entries
                if item.agent_id == "conclusion.position_generator"
            )
            self.assertFalse(entry.drifted)
            self.assertEqual(entry.runtime_model, "mock")
            self.assertEqual(entry.resolved_model, "verified/replacement")

    def test_saved_retrieval_is_reused_for_one_shot_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, repository, provider = self.failed_one_task_workflow(
                Path(temporary)
            )
            manager, retrieval_provider = self.repaired_manager(repository, provider)
            audit = manager.inspect_runtime_model_recovery(state.workflow_id)
            self.assertEqual(audit["eligible_before_capability_check"], 1)
            self.assertEqual(retrieval_provider.calls, 0)

            result = asyncio.run(
                manager.recover_runtime_model_drift(
                    state.workflow_id,
                    capability_client=CompatibleClient(),
                )
            )
            self.assertEqual(result.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
            self.assertFalse(result.deliberation_sent)
            self.assertEqual(retrieval_provider.calls, 0)
            self.assertEqual(provider.agent_calls, [AGENT_ID])
            self.assertEqual(provider.calls, ["ResearchResult", "ResearchQualityReviewOutput"])
            task_id = state.research_tasks[0]["task_id"]
            authorization = manager.runtime_model_repair_store.for_original_task(
                workflow_id=state.workflow_id,
                provider_id="mock",
                original_task_id=task_id,
            )
            self.assertEqual(authorization.status, RuntimeModelRepairStatus.CONSUMED.value)
            self.assertTrue(authorization.repair_task_id.endswith("_runtime_model_repair_1"))
            self.assertTrue(
                manager.runtime_model_repair_store.reservation_path(
                    provider_id="mock",
                    workflow_id=state.workflow_id,
                    task_id=authorization.repair_task_id,
                ).exists()
            )
            provider.calls.clear()
            provider.agent_calls.clear()
            with self.assertRaisesRegex(ValueError, "failed or incomplete"):
                asyncio.run(
                    manager.recover_runtime_model_drift(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(provider.calls, [])

    def test_missing_saved_retrieval_fails_closed_without_search_or_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, repository, provider = self.failed_one_task_workflow(
                Path(temporary)
            )
            for path in (Path(temporary) / "retrieval_contexts").rglob("*.json"):
                path.unlink()
            manager, retrieval_provider = self.repaired_manager(repository, provider)
            with self.assertRaisesRegex(ValueError, "Automatic retrieval is forbidden"):
                asyncio.run(
                    manager.recover_runtime_model_drift(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(retrieval_provider.calls, 0)
            self.assertEqual(provider.calls, [])
            self.assertEqual(
                list(
                    (Path(temporary) / "provider_runtime_model_repair_authorizations").rglob(
                        "*.json"
                    )
                ),
                [],
            )

    def test_post_commit_state_save_fault_reuses_result_without_reasoning_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, repository, provider = self.failed_one_task_workflow(
                Path(temporary)
            )
            manager, retrieval_provider = self.repaired_manager(repository, provider)
            original_save = repository.save
            injected = False

            def save_then_raise(saved_state):
                nonlocal injected
                original_save(saved_state)
                if (
                    not injected
                    and saved_state.research_report is None
                    and saved_state.agent_results
                ):
                    injected = True
                    raise OSError("post-commit fault injection")

            repository.save = save_then_raise
            with self.assertRaisesRegex(OSError, "post-commit"):
                asyncio.run(
                    manager.recover_runtime_model_drift(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            repository.save = original_save
            self.assertEqual(retrieval_provider.calls, 0)
            self.assertEqual(provider.calls, ["ResearchResult"])
            provider.calls.clear()
            provider.agent_calls.clear()

            recovered = asyncio.run(
                manager.recover_runtime_model_drift(
                    state.workflow_id,
                    capability_client=CompatibleClient(),
                )
            )
            self.assertEqual(recovered.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
            self.assertFalse(recovered.deliberation_sent)
            self.assertEqual(retrieval_provider.calls, 0)
            self.assertNotIn("ResearchResult", provider.calls)
            self.assertEqual(provider.calls, ["ResearchQualityReviewOutput"])

    def test_partial_success_is_reused_and_failed_repair_cannot_repair_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, repository, provider = self.failed_workflow(
                Path(temporary),
                "ACADEMIC",
                "GOVERNMENT",
            )
            provider.fail_agent_ids = {GOVERNMENT_AGENT_ID}
            provider.calls.clear()
            provider.agent_calls.clear()
            models = {
                AGENT_ID: CURRENT_MODEL,
                GOVERNMENT_AGENT_ID: CURRENT_MODEL,
                "researcher.quality_reviewer": "mock",
            }
            registry = ResearcherRegistry(provider, models, demo_safe_mode=True)
            manager = ResearcherManager(
                registry,
                repository,
                demo_safe_mode=True,
            )
            first = asyncio.run(
                manager.recover_runtime_model_drift(
                    state.workflow_id,
                    capability_client=CompatibleClient(),
                )
            )
            self.assertEqual(first.status, "FAILED")
            academic_task = next(
                item["task_id"]
                for item in state.research_tasks
                if item["target_agent_id"] == AGENT_ID
            )
            self.assertIn(academic_task, manager._completed_research_task_ids(first))
            self.assertEqual(provider.agent_calls, [AGENT_ID, GOVERNMENT_AGENT_ID])

            provider.fail_agent_ids.clear()
            provider.calls.clear()
            provider.agent_calls.clear()
            next_registry = ResearcherRegistry(provider, models, demo_safe_mode=True)
            next_manager = ResearcherManager(
                next_registry,
                repository,
                demo_safe_mode=True,
            )
            with self.assertRaisesRegex(ValueError, "already consumed"):
                asyncio.run(
                    next_manager.recover_runtime_model_drift(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(provider.calls, [])
            self.assertIn(
                academic_task,
                next_manager._completed_research_task_ids(
                    repository.load(state.workflow_id)
                ),
            )


if __name__ == "__main__":
    unittest.main()
