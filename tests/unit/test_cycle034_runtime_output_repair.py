from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from common.provider_runtime_output_repair import (
    RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
    RuntimeModelOutputRepairStatus,
)
from providers.mock_provider import MockModelProvider
from providers.openrouter_capabilities import (
    ModelCapabilityResult,
    ModelCapabilityStatus,
)
from researcher.manager import EXCERPT_CONTRACT_ERROR_PREFIX, ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.research_task import ResearchTask
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from tests.researcher_helpers import make_handoff, make_plan


OLD_MODEL = "perplexity/sonar-deep-research"
CURRENT_MODEL = "google/gemini-3.7-flash"
AGENT_ID = "researcher.government_researcher"


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


class Cycle034RuntimeOutputRepairTests(unittest.TestCase):
    @staticmethod
    def _government_plan():
        payload = make_plan().model_dump(mode="json")
        payload["research_questions"] = [
            {
                **payload["research_questions"][0],
                "research_targets": ["GOVERNMENT"],
            }
        ]
        return type(make_plan()).model_validate(payload)

    def _historical_excerpt_failure(self, root: Path):
        repository = ResearcherWorkflowRepository(root)
        provider = MockModelProvider(
            fail_agent_ids={AGENT_ID},
            reservation_root=root / "provider_call_reservations",
        )
        initial_registry = ResearcherRegistry(
            provider,
            {AGENT_ID: OLD_MODEL},
            demo_safe_mode=True,
        )
        initial_manager = ResearcherManager(
            initial_registry,
            repository,
            demo_safe_mode=True,
        )
        state = asyncio.run(
            initial_manager.start_from_message(
                make_handoff(self._government_plan())
            )
        )
        for index, message in enumerate(state.message_history):
            if message.message_type != "error":
                continue
            state.message_history[index] = message.model_copy(
                update={
                    "payload": {
                        **message.payload,
                        "error_code": "ProviderCapabilityError",
                        "error_class": "ProviderCapabilityError",
                        "http_status": 404,
                        "model_id": OLD_MODEL,
                        "automatic_retry_allowed": False,
                    }
                }
            )
        repository.save(state)

        registry = ResearcherRegistry(
            provider,
            {
                AGENT_ID: CURRENT_MODEL,
                "researcher.quality_reviewer": "mock",
            },
            demo_safe_mode=True,
        )
        manager = ResearcherManager(registry, repository, demo_safe_mode=True)
        failed = asyncio.run(
            manager.recover_runtime_model_drift(
                state.workflow_id,
                capability_client=CompatibleClient(),
            )
        )
        self.assertEqual(failed.status, "FAILED")
        task = ResearchTask.model_validate(failed.research_tasks[0])
        retrieval = manager._saved_retrieval_evidence(failed, task)
        self.assertIsNotNone(retrieval)
        source_id = retrieval.context.sources[0].source_id
        for index in range(len(failed.message_history) - 1, -1, -1):
            message = failed.message_history[index]
            if (
                message.message_type == "error"
                and message.payload.get("task_id", "").endswith(
                    "_runtime_model_repair_1"
                )
            ):
                failed.message_history[index] = message.model_copy(
                    update={
                        "payload": {
                            **message.payload,
                            "error_code": "NonRetryableAgentError",
                            "error_class": "NonRetryableAgentError",
                            "http_status": None,
                            "model_id": CURRENT_MODEL,
                            "message": EXCERPT_CONTRACT_ERROR_PREFIX + source_id,
                            "root_exception_message": (
                                EXCERPT_CONTRACT_ERROR_PREFIX + source_id
                            ),
                        }
                    }
                )
                break
        else:
            self.fail("runtime repair error was not saved")
        repository.save(failed)
        provider.calls.clear()
        provider.agent_calls.clear()
        return manager, provider, task, retrieval

    def test_one_new_identity_repairs_excerpt_then_resumes_without_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, task, retrieval = self._historical_excerpt_failure(
                Path(temporary)
            )
            retrieval_provider = manager.registry.get(
                AGENT_ID
            ).retrieval_coordinator.provider
            retrieval_calls_before = retrieval_provider.calls
            # Use the workflow bound to the saved immutable context.
            workflow_id = retrieval.context.workflow_id
            audit = manager.inspect_runtime_output_repair(workflow_id)
            self.assertEqual(audit["eligible_count"], 1)
            self.assertEqual(audit["planned_output_repair_calls"], 1)

            provider.fail_agent_ids.clear()
            result = asyncio.run(
                manager.recover_runtime_output_contract(
                    workflow_id,
                    capability_client=CompatibleClient(),
                )
            )
            self.assertEqual(result.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
            self.assertFalse(result.deliberation_sent)
            self.assertEqual(retrieval_provider.calls, retrieval_calls_before)
            self.assertEqual(provider.agent_calls, [AGENT_ID])
            self.assertEqual(
                provider.calls,
                ["ResearchResult", "ResearchQualityReviewOutput"],
            )
            authorization = (
                manager.runtime_model_output_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id="mock",
                    original_task_id=task.task_id,
                )
            )
            self.assertEqual(
                authorization.status,
                RuntimeModelOutputRepairStatus.CONSUMED.value,
            )
            self.assertEqual(
                authorization.output_repair_task_id,
                task.task_id + RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
            )
            self.assertTrue(
                manager.runtime_model_output_repair_store.reservation_path(
                    provider_id="mock",
                    workflow_id=workflow_id,
                    task_id=authorization.output_repair_task_id,
                ).exists()
            )

    def test_hash_change_blocks_before_reservation_or_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, task, retrieval = self._historical_excerpt_failure(
                Path(temporary)
            )
            path = (
                manager.repository.data_dir
                / "retrieval_contexts"
                / retrieval.context.workflow_id
                / f"{retrieval.context.retrieval_id}.json"
            )
            path.write_bytes(path.read_bytes() + b" ")
            provider.fail_agent_ids.clear()
            with self.assertRaisesRegex(
                ValueError,
                "exactly one eligible|RETRIEVAL_IDENTITY_CHANGED",
            ):
                asyncio.run(
                    manager.recover_runtime_output_contract(
                        retrieval.context.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(provider.calls, [])
            self.assertEqual(
                list(
                    (
                        manager.repository.data_dir
                        / "provider_runtime_output_repair_authorizations"
                    ).rglob("*.json")
                ),
                [],
            )

    def test_consumed_output_repair_cannot_be_sent_again_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, task, retrieval = self._historical_excerpt_failure(
                Path(temporary)
            )
            first = asyncio.run(
                manager.recover_runtime_output_contract(
                    retrieval.context.workflow_id,
                    capability_client=CompatibleClient(),
                )
            )
            self.assertEqual(first.status, "FAILED")
            self.assertEqual(provider.calls, ["ResearchResult"])
            provider.fail_agent_ids.clear()
            provider.calls.clear()
            provider.agent_calls.clear()
            with self.assertRaisesRegex(ValueError, "exactly one eligible"):
                asyncio.run(
                    manager.recover_runtime_output_contract(
                        retrieval.context.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
