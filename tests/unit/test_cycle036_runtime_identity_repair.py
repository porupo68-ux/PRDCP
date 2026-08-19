from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from common.provider_runtime_output_repair import (
    RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX,
    RuntimeAdapterRepairStatus,
)
from researcher.manager import SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX
from tests.unit.test_cycle034_runtime_output_repair import AGENT_ID, CompatibleClient
from tests.unit.test_cycle035_runtime_adapter_repair import (
    Cycle035RuntimeAdapterRepairTests,
)


class Cycle036RuntimeIdentityRepairTests(Cycle035RuntimeAdapterRepairTests):
    def _historical_composite_identity_failure(self, root: Path):
        manager, provider, task, retrieval = self._historical_identity_failure(root)
        failed = asyncio.run(
            manager.recover_runtime_adapter_contract(
                retrieval.context.workflow_id,
                capability_client=CompatibleClient(),
            )
        )
        self.assertEqual(failed.status, "FAILED")
        source_id = retrieval.context.sources[0].source_id
        for index in range(len(failed.message_history) - 1, -1, -1):
            message = failed.message_history[index]
            if (
                message.message_type == "error"
                and message.payload.get("task_id", "").endswith(
                    "_runtime_adapter_repair_1"
                )
            ):
                detail = (
                    SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX
                    + source_id
                    + ": 内閣官房 / 内閣府知的財産戦略推進事務局"
                )
                failed.message_history[index] = message.model_copy(
                    update={
                        "payload": {
                            **message.payload,
                            "error_code": "NonRetryableAgentError",
                            "error_class": "NonRetryableAgentError",
                            "http_status": None,
                            "message": detail,
                            "root_exception_message": detail,
                        }
                    }
                )
                break
        else:
            self.fail("runtime adapter repair error was not saved")
        manager.repository.save(failed)
        provider.calls.clear()
        provider.agent_calls.clear()
        return manager, provider, task, retrieval

    def test_identity_repair_uses_new_identity_and_zero_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, task, retrieval = (
                self._historical_composite_identity_failure(Path(temporary))
            )
            workflow_id = retrieval.context.workflow_id
            retrieval_provider = manager.registry.get(
                AGENT_ID
            ).retrieval_coordinator.provider
            retrieval_calls_before = retrieval_provider.calls
            audit = manager.inspect_runtime_identity_repair(workflow_id)
            self.assertEqual(audit["eligible_count"], 1)
            self.assertEqual(audit["planned_identity_repair_calls"], 1)
            provider.fail_agent_ids.clear()
            result = asyncio.run(
                manager.recover_runtime_identity_contract(
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
            authorization = manager.runtime_identity_repair_store.for_original_task(
                workflow_id=workflow_id,
                provider_id="mock",
                original_task_id=task.task_id,
            )
            self.assertEqual(
                authorization.status,
                RuntimeAdapterRepairStatus.CONSUMED.value,
            )
            self.assertEqual(
                authorization.repair_task_id,
                task.task_id + RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX,
            )

    def test_consumed_identity_failure_cannot_be_called_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, _task, retrieval = (
                self._historical_composite_identity_failure(Path(temporary))
            )
            workflow_id = retrieval.context.workflow_id
            first = asyncio.run(
                manager.recover_runtime_identity_contract(
                    workflow_id,
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
                    manager.recover_runtime_identity_contract(
                        workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(provider.calls, [])

    def test_non_composite_identity_failure_is_not_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, _task, retrieval = (
                self._historical_composite_identity_failure(Path(temporary))
            )
            state = manager.repository.load(retrieval.context.workflow_id)
            for index in range(len(state.message_history) - 1, -1, -1):
                message = state.message_history[index]
                if message.payload.get("task_id", "").endswith(
                    "_runtime_adapter_repair_1"
                ) and message.message_type == "error":
                    detail = message.payload["message"].replace(
                        "内閣官房 / 内閣府知的財産戦略推進事務局",
                        "Unrelated Organization",
                    )
                    state.message_history[index] = message.model_copy(
                        update={
                            "payload": {
                                **message.payload,
                                "message": detail,
                                "root_exception_message": detail,
                            }
                        }
                    )
                    break
            manager.repository.save(state)
            audit = manager.inspect_runtime_identity_repair(
                retrieval.context.workflow_id
            )
            self.assertEqual(audit["eligible_count"], 0)
            self.assertIn(
                "FAILURE_IS_NOT_COMPOSITE_IDENTITY",
                next(item for item in audit["tasks"] if item["task_id"] == _task.task_id)[
                    "blockers"
                ],
            )
            self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
