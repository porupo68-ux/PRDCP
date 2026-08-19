from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from common.models.errors import NonRetryableAgentError
from common.models.pmp import MessageStatus, MessageType, PMPContext, PMPMessage, PMPMetadata
from common.retrieval_reconstruction import (
    RETRIEVAL_RECONSTRUCTION_SUFFIX,
    RetrievalReconstructionStatus,
)
from common.provider_runtime_model_repair import RUNTIME_MODEL_REPAIR_SUFFIX
from providers.mock_provider import MockModelProvider
from providers.openrouter_capabilities import (
    ModelCapabilityResult,
    ModelCapabilityStatus,
)
from researcher.manager import ResearcherManager
from researcher.registry import ResearcherRegistry
from researcher.schemas.research_task import ResearchTask
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from tests.researcher_helpers import make_handoff, make_plan


OLD_MODEL = "perplexity/sonar-deep-research"
CURRENT_MODEL = "google/gemini-3.7-flash"
ACADEMIC_AGENT = "researcher.academic_researcher"
GOVERNMENT_AGENT = "researcher.government_researcher"


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


class Cycle033RetrievalReconstructionTests(unittest.TestCase):
    @staticmethod
    def _plan_for_targets(*targets: str):
        payload = make_plan().model_dump(mode="json")
        payload["research_questions"] = [
            {
                **payload["research_questions"][0],
                "research_targets": list(targets),
            }
        ]
        return type(make_plan()).model_validate(payload)

    def _failed_workflow(self, root: Path, *targets: str):
        agent_map = {
            "ACADEMIC": ACADEMIC_AGENT,
            "GOVERNMENT": GOVERNMENT_AGENT,
        }
        failed_agents = {agent_map[target] for target in targets}
        repository = ResearcherWorkflowRepository(root)
        provider = MockModelProvider(fail_agent_ids=failed_agents)
        provider.reservation_root = root / "provider_call_reservations"
        registry = ResearcherRegistry(
            provider,
            {agent_id: OLD_MODEL for agent_id in failed_agents},
            demo_safe_mode=True,
        )
        manager = ResearcherManager(registry, repository, demo_safe_mode=True)
        state = asyncio.run(
            manager.start_from_message(
                make_handoff(self._plan_for_targets(*targets))
            )
        )
        self.assertEqual(state.status, "FAILED")
        for index, message in enumerate(state.message_history):
            if message.message_type != MessageType.ERROR.value:
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
        # Cycle 033 starts from the production failure mode: the LLM reservation
        # and ERROR PMP exist, but no Researcher Retrieval Context was persisted.
        context_root = root / "retrieval_contexts" / state.workflow_id
        if context_root.exists():
            for path in context_root.glob("*.json"):
                path.unlink()
        return state, repository, provider

    @staticmethod
    def _repaired_manager(repository, provider):
        provider.fail_agent_ids.clear()
        provider.calls.clear()
        provider.agent_calls.clear()
        registry = ResearcherRegistry(
            provider,
            {
                ACADEMIC_AGENT: CURRENT_MODEL,
                GOVERNMENT_AGENT: CURRENT_MODEL,
                "researcher.quality_reviewer": "mock",
            },
            demo_safe_mode=True,
        )
        manager = ResearcherManager(registry, repository, demo_safe_mode=True)
        retrieval_provider = registry.get(ACADEMIC_AGENT).retrieval_coordinator.provider
        return manager, retrieval_provider

    def test_new_identity_reconstructs_once_then_reuses_context_for_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, repository, provider = self._failed_workflow(root, "ACADEMIC")
            task_id = state.research_tasks[0]["task_id"]
            old_reservation = (
                root / "provider_call_reservations" / "mock" / state.workflow_id
                / f"{task_id}.json"
            )
            old_reservation_hash = old_reservation.read_bytes()
            manager, retrieval_provider = self._repaired_manager(repository, provider)

            before = manager.inspect_retrieval_reconstruction(state.workflow_id)
            self.assertEqual(before["planned_retrieval_calls"], 1)
            self.assertEqual(before["planned_reasoning_calls"], 1)
            result = asyncio.run(
                manager.reconstruct_missing_retrieval_and_recover(
                    state.workflow_id,
                    capability_client=CompatibleClient(),
                )
            )

            self.assertEqual(result.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
            self.assertFalse(result.deliberation_sent)
            self.assertEqual(retrieval_provider.calls, 1)
            self.assertEqual(provider.agent_calls, [ACADEMIC_AGENT])
            self.assertEqual(provider.calls, ["ResearchResult", "ResearchQualityReviewOutput"])
            authorization = manager.retrieval_reconstruction_store.for_original_task(
                workflow_id=state.workflow_id,
                retrieval_provider_id="mock",
                original_task_id=task_id,
            )
            self.assertEqual(
                authorization.status,
                RetrievalReconstructionStatus.CONSUMED.value,
            )
            self.assertEqual(
                authorization.reconstruction_task_id,
                task_id + RETRIEVAL_RECONSTRUCTION_SUFFIX,
            )
            self.assertNotEqual(authorization.reconstruction_task_id, task_id)
            self.assertIsNotNone(authorization.retrieval_context_sha256)
            self.assertEqual(old_reservation.read_bytes(), old_reservation_hash)
            runtime_authorization = manager.runtime_model_repair_store.for_original_task(
                workflow_id=state.workflow_id,
                provider_id="mock",
                original_task_id=task_id,
            )
            self.assertEqual(
                runtime_authorization.retrieval_id,
                authorization.retrieval_id,
            )

    def test_consumed_search_failure_is_not_called_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, repository, provider = self._failed_workflow(root, "ACADEMIC")
            task_id = state.research_tasks[0]["task_id"]
            manager, retrieval_provider = self._repaired_manager(repository, provider)
            retrieval_provider.failure = NonRetryableAgentError(
                "injected retrieval failure",
                automatic_retry_allowed=False,
            )
            with self.assertRaisesRegex(NonRetryableAgentError, "injected"):
                asyncio.run(
                    manager.reconstruct_missing_retrieval_and_recover(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(retrieval_provider.calls, 1)
            authorization = manager.retrieval_reconstruction_store.for_original_task(
                workflow_id=state.workflow_id,
                retrieval_provider_id="mock",
                original_task_id=task_id,
            )
            self.assertEqual(
                authorization.status,
                RetrievalReconstructionStatus.CONSUMED.value,
            )
            self.assertIsNone(authorization.retrieval_context_sha256)

            retrieval_provider.failure = None
            with self.assertRaisesRegex(ValueError, "Context is missing"):
                asyncio.run(
                    manager.reconstruct_missing_retrieval_and_recover(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(retrieval_provider.calls, 1)
            self.assertEqual(provider.calls, [])

    def test_context_write_checkpoint_recovers_without_second_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, repository, provider = self._failed_workflow(root, "ACADEMIC")
            manager, retrieval_provider = self._repaired_manager(repository, provider)
            original_record = manager.retrieval_reconstruction_store.record_context
            injected = False

            def fail_before_hash_record(authorization):
                nonlocal injected
                if not injected:
                    injected = True
                    raise OSError("post-context checkpoint fault")
                return original_record(authorization)

            manager.retrieval_reconstruction_store.record_context = fail_before_hash_record
            with self.assertRaisesRegex(OSError, "post-context"):
                asyncio.run(
                    manager.reconstruct_missing_retrieval_and_recover(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(retrieval_provider.calls, 1)
            manager.retrieval_reconstruction_store.record_context = original_record

            result = asyncio.run(
                manager.reconstruct_missing_retrieval_and_recover(
                    state.workflow_id,
                    capability_client=CompatibleClient(),
                )
            )
            self.assertEqual(result.status, "WAITING_HUMAN_EVIDENCE_REVIEW")
            self.assertFalse(result.deliberation_sent)
            self.assertEqual(retrieval_provider.calls, 1)
            self.assertEqual(provider.calls, ["ResearchResult", "ResearchQualityReviewOutput"])

    def test_saved_reconstruction_hash_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, repository, provider = self._failed_workflow(root, "ACADEMIC")
            task_id = state.research_tasks[0]["task_id"]
            manager, _retrieval_provider = self._repaired_manager(repository, provider)
            asyncio.run(
                manager.reconstruct_missing_retrieval_and_recover(
                    state.workflow_id,
                    capability_client=CompatibleClient(),
                )
            )
            authorization = manager.retrieval_reconstruction_store.for_original_task(
                workflow_id=state.workflow_id,
                retrieval_provider_id="mock",
                original_task_id=task_id,
            )
            context_path = manager.retrieval_reconstruction_store.context_path(
                workflow_id=state.workflow_id,
                retrieval_id=authorization.retrieval_id,
            )
            context_path.write_bytes(context_path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "hash changed"):
                manager.retrieval_reconstruction_store.record_context(authorization)

    def test_partial_reconstruction_stops_and_preserves_first_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, repository, provider = self._failed_workflow(
                root,
                "ACADEMIC",
                "GOVERNMENT",
            )
            manager, retrieval_provider = self._repaired_manager(repository, provider)
            original_search = retrieval_provider.search

            async def fail_second_search(**kwargs):
                if retrieval_provider.calls == 1:
                    retrieval_provider.calls += 1
                    raise NonRetryableAgentError(
                        "second search fault",
                        automatic_retry_allowed=False,
                    )
                return await original_search(**kwargs)

            retrieval_provider.search = fail_second_search
            with self.assertRaisesRegex(NonRetryableAgentError, "second search"):
                asyncio.run(
                    manager.reconstruct_missing_retrieval_and_recover(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(retrieval_provider.calls, 2)
            context_paths = list(
                (root / "retrieval_contexts" / state.workflow_id).glob("*.json")
            )
            self.assertEqual(len(context_paths), 1)
            self.assertEqual(provider.calls, [])

            # A later run must not search the first task again.  The consumed
            # second task remains ambiguous, so no third provider call occurs.
            retrieval_provider.search = original_search
            with self.assertRaisesRegex(ValueError, "Context is missing"):
                asyncio.run(
                    manager.reconstruct_missing_retrieval_and_recover(
                        state.workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(retrieval_provider.calls, 2)

    def test_retrieval_override_without_runtime_repair_is_rejected_pre_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, repository, provider = self._failed_workflow(root, "ACADEMIC")
            manager, retrieval_provider = self._repaired_manager(repository, provider)
            task = ResearchTask.model_validate(state.research_tasks[0])
            message = PMPMessage.create(
                workflow_id=state.workflow_id,
                sender_agent_id="researcher.manager",
                receiver_agent_id=ACADEMIC_AGENT,
                message_type=MessageType.TASK,
                objective="spoofed retrieval override",
                payload=task.model_dump(mode="json"),
                context=PMPContext(current_stage=ACADEMIC_AGENT),
                metadata=PMPMetadata(
                    status=MessageStatus.QUEUED,
                    extensions={
                        "retrieval_task_id": task.task_id
                        + RETRIEVAL_RECONSTRUCTION_SUFFIX,
                    },
                ),
            )
            response = asyncio.run(manager.registry.get(ACADEMIC_AGENT).execute(message))
            self.assertEqual(response.message_type, MessageType.ERROR.value)
            self.assertIn("RETRIEVAL_CONTEXT_OVERRIDE_REJECTED", response.payload["message"])
            self.assertEqual(retrieval_provider.calls, 0)
            self.assertEqual(provider.calls, [])

    def test_runtime_repair_message_binds_both_one_shot_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state, repository, provider = self._failed_workflow(
                Path(temporary),
                "ACADEMIC",
            )
            manager, _retrieval_provider = self._repaired_manager(repository, provider)
            task = ResearchTask.model_validate(state.research_tasks[0])
            request = manager._create_task_message(
                state,
                task,
                is_revision=False,
                provider_task_id=task.task_id + RUNTIME_MODEL_REPAIR_SUFFIX,
                retrieval_task_id=task.task_id + RETRIEVAL_RECONSTRUCTION_SUFFIX,
            )
            self.assertEqual(
                request.metadata.extensions["provider_task_id"],
                task.task_id + RUNTIME_MODEL_REPAIR_SUFFIX,
            )
            self.assertEqual(
                request.metadata.extensions["retrieval_task_id"],
                task.task_id + RETRIEVAL_RECONSTRUCTION_SUFFIX,
            )


if __name__ == "__main__":
    unittest.main()
