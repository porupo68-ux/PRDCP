from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from common.provider_runtime_output_repair import (
    RUNTIME_ADAPTER_REPAIR_SUFFIX,
    RuntimeAdapterRepairStatus,
)
from researcher.manager import SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX
from tests.unit.test_cycle034_runtime_output_repair import (
    AGENT_ID,
    CompatibleClient,
    Cycle034RuntimeOutputRepairTests,
)


class Cycle035RuntimeAdapterRepairTests(Cycle034RuntimeOutputRepairTests):
    def _historical_identity_failure(self, root: Path):
        manager, provider, task, retrieval = self._historical_excerpt_failure(root)
        failed = asyncio.run(
            manager.recover_runtime_output_contract(
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
                    "_runtime_model_output_repair_1"
                )
            ):
                detail = (
                    SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX
                    + source_id
                    + ": Japan, 内閣府知的財産戦略推進事務局"
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
            self.fail("runtime output repair error was not saved")
        manager.repository.save(failed)
        provider.calls.clear()
        provider.agent_calls.clear()
        return manager, provider, task, retrieval

    def test_adapter_repair_uses_new_identity_and_zero_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, task, retrieval = self._historical_identity_failure(
                Path(temporary)
            )
            workflow_id = retrieval.context.workflow_id
            retrieval_provider = manager.registry.get(
                AGENT_ID
            ).retrieval_coordinator.provider
            retrieval_calls_before = retrieval_provider.calls
            audit = manager.inspect_runtime_adapter_repair(workflow_id)
            self.assertEqual(audit["eligible_count"], 1)
            provider.fail_agent_ids.clear()
            result = asyncio.run(
                manager.recover_runtime_adapter_contract(
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
            authorization = manager.runtime_adapter_repair_store.for_original_task(
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
                task.task_id + RUNTIME_ADAPTER_REPAIR_SUFFIX,
            )

    def test_consumed_adapter_failure_cannot_be_called_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager, provider, _task, retrieval = self._historical_identity_failure(
                Path(temporary)
            )
            workflow_id = retrieval.context.workflow_id
            first = asyncio.run(
                manager.recover_runtime_adapter_contract(
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
                    manager.recover_runtime_adapter_contract(
                        workflow_id,
                        capability_client=CompatibleClient(),
                    )
                )
            self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()
