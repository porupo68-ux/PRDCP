from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from common.provider_retry import (
    ProviderRetryAuthorizationStore,
    ProviderRetryStatus,
)


class ProviderRetryAuthorizationStoreTests(unittest.TestCase):
    workflow_id = "workflow-provider-retry"
    provider_id = "openrouter"
    agent_id = "researcher.quality_reviewer"
    task_id = "research_quality_review_external_1_contract_v2"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.store = ProviderRetryAuthorizationStore(self.data_dir)
        reservation_path = (
            self.data_dir
            / "provider_call_reservations"
            / self.provider_id
            / self.workflow_id
            / f"{self.task_id}.json"
        )
        reservation_path.parent.mkdir(parents=True)
        reservation_path.write_text(
            json.dumps(
                {
                    "workflow_id": self.workflow_id,
                    "task_id": self.task_id,
                    "agent_id": self.agent_id,
                    "provider": "OpenRouterModelProvider",
                    "model_id": "test-model",
                    "reserved_at": "2026-08-16T00:00:00+00:00",
                }
            ),
            encoding="utf-8",
        )

    def authorize(self, *, error_class: str = "RetryableAgentError"):
        return self.store.authorize_once(
            workflow_id=self.workflow_id,
            provider_id=self.provider_id,
            agent_id=self.agent_id,
            original_task_id=self.task_id,
            source_error_message_id="message-retryable-error",
            source_error_class=error_class,
        )

    def test_authorization_is_idempotent_until_consumed(self) -> None:
        first = self.authorize()
        second = self.authorize()

        self.assertEqual(first.authorization_id, second.authorization_id)
        self.assertEqual(first.status, ProviderRetryStatus.PENDING.value)
        self.assertEqual(
            first.retry_task_id,
            f"{self.task_id}_operator_retry_1",
        )

    def test_consumed_authorization_cannot_be_reissued(self) -> None:
        authorization = self.authorize()
        retry_reservation = (
            self.data_dir
            / "provider_call_reservations"
            / self.provider_id
            / self.workflow_id
            / f"{authorization.retry_task_id}.json"
        )
        retry_reservation.write_text("{}", encoding="utf-8")

        consumed = self.store.consume(
            authorization,
            reservation_path=retry_reservation,
        )

        self.assertEqual(consumed.status, ProviderRetryStatus.CONSUMED.value)
        with self.assertRaisesRegex(ValueError, "already consumed"):
            self.authorize()

    def test_unclassified_errors_cannot_be_authorized(self) -> None:
        with self.assertRaisesRegex(ValueError, "request/output contract failure"):
            self.authorize(error_class="ApplicationError")

    def test_persisted_provider_request_schema_failure_is_authorizable(self) -> None:
        authorization = self.authorize(error_class="ProviderRequestSchemaError")

        self.assertEqual(
            authorization.source_error_class,
            "ProviderRequestSchemaError",
        )

    def test_retry_of_retry_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot authorize another retry"):
            self.store.authorize_once(
                workflow_id=self.workflow_id,
                provider_id=self.provider_id,
                agent_id=self.agent_id,
                original_task_id=f"{self.task_id}_operator_retry_1",
                source_error_message_id="message-second-error",
                source_error_class="RetryableAgentError",
            )

    def test_persisted_provider_payload_validation_failure_is_authorizable(self) -> None:
        authorization = self.authorize(error_class="PayloadValidationError")

        self.assertEqual(
            authorization.source_error_class,
            "PayloadValidationError",
        )


if __name__ == "__main__":
    unittest.main()
