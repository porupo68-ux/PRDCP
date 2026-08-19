import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from common.provider_contract_repair import ProviderContractRepairAuthorization
from common.provider_model_compatibility import ProviderModelCompatibilityStore


class ProviderModelCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _authorization(*, status: str = "CONSUMED"):
        return ProviderContractRepairAuthorization(
            authorization_id="authorization-1",
            workflow_id="workflow-1",
            provider_id="openrouter",
            agent_id="conclusion.position_generator",
            original_task_id="position-task",
            retry_task_id="position-task_operator_retry_1",
            repair_task_id="position-task_provider_contract_repair_1",
            source_retry_authorization_id="retry-authorization-1",
            source_error_message_id="error-message-1",
            source_error_class="ProviderResponseContractError",
            failed_model_id="qwen/qwen3.5-flash-02-23",
            repair_model_id="openai/gpt-5-mini",
            authorized_by="cli.operator",
            status=status,
            authorized_at=datetime.now(timezone.utc),
            consumed_at=(datetime.now(timezone.utc) if status == "CONSUMED" else None),
            reservation_path=("reservation.json" if status == "CONSUMED" else None),
        )

    def test_verified_binding_requires_exact_configured_model_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ProviderModelCompatibilityStore(Path(temporary))
            binding = store.record_verified_repair(
                self._authorization(),
                output_schema_id=(
                    "conclusion.schemas.position_candidate.PositionGenerationResult"
                ),
                result_task_id="position-task_provider_contract_repair_1",
                result_message_id="result-message-1",
            )

            resolved = store.resolve(
                provider_id="openrouter",
                agent_id="conclusion.position_generator",
                output_schema_id=(
                    "conclusion.schemas.position_candidate.PositionGenerationResult"
                ),
                configured_model_id="qwen/qwen3.5-flash-02-23",
            )
            changed_config = store.resolve(
                provider_id="openrouter",
                agent_id="conclusion.position_generator",
                output_schema_id=(
                    "conclusion.schemas.position_candidate.PositionGenerationResult"
                ),
                configured_model_id="openai/gpt-5.5",
            )

            self.assertEqual(resolved, binding)
            self.assertEqual(resolved.compatible_model_id, "openai/gpt-5-mini")
            self.assertIsNone(changed_config)
            self.assertEqual(len(store.list_verified(provider_id="openrouter")), 1)

    def test_pending_repair_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ProviderModelCompatibilityStore(Path(temporary))
            with self.assertRaisesRegex(ValueError, "consumed repair"):
                store.record_verified_repair(
                    self._authorization(status="PENDING"),
                    output_schema_id="schema.Output",
                    result_task_id="position-task_provider_contract_repair_1",
                )


if __name__ == "__main__":
    unittest.main()
