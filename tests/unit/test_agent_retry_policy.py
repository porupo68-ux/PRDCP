from __future__ import annotations

import json
import unittest

from pydantic import BaseModel

from common.agents.base import StructuredAgent
from common.models.errors import (
    AgentExecutionError,
    NonRetryableAgentError,
    PayloadValidationError,
    RetryableAgentError,
)
from common.models.pmp import MessageType, PMPMessage
from common.validation import PayloadValidator


class _Payload(BaseModel):
    answer: str


class _SequencedProvider:
    def __init__(self, outcomes: list[dict | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def generate_structured(self, **_kwargs: object) -> dict:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _agent(
    provider: _SequencedProvider,
    *,
    demo_safe_mode: bool = False,
) -> StructuredAgent:
    agent = object.__new__(StructuredAgent)
    agent.provider = provider
    agent.model = "test/model"
    agent.payload_validator = PayloadValidator()
    agent.agent_id = "researcher.test_agent"
    agent.manager_agent_id = "researcher.manager"
    agent.error_next_stage = None
    agent.output_schema = _Payload
    agent.demo_safe_mode = demo_safe_mode
    return agent


class AgentRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_error_retries_once_then_succeeds(self) -> None:
        provider = _SequencedProvider(
            [RetryableAgentError("temporary provider failure"), {"answer": "ok"}]
        )

        result, retry_count = await _agent(provider).run(
            _Payload(answer="input"),
            system_prompt="test",
            max_technical_retries=5,
            timeout_seconds=1,
        )

        self.assertEqual(result.answer, "ok")
        self.assertEqual(retry_count, 1)
        self.assertEqual(provider.calls, 2)

    async def test_transient_error_stops_after_one_retry(self) -> None:
        provider = _SequencedProvider([RetryableAgentError("temporary provider failure")])

        with self.assertRaises(RetryableAgentError) as raised:
            await _agent(provider).run(
                _Payload(answer="input"),
                system_prompt="test",
                max_technical_retries=5,
                timeout_seconds=1,
            )

        self.assertEqual(raised.exception.retry_count, 1)
        self.assertEqual(provider.calls, 2)

    async def test_demo_safe_mode_disables_retryable_error_retry(self) -> None:
        provider = _SequencedProvider(
            [RetryableAgentError("temporary provider failure"), {"answer": "unused"}]
        )

        with self.assertRaises(RetryableAgentError) as raised:
            await _agent(provider, demo_safe_mode=True).run(
                _Payload(answer="input"),
                system_prompt="test",
                max_technical_retries=5,
                timeout_seconds=1,
            )

        self.assertEqual(raised.exception.retry_count, 0)
        self.assertEqual(provider.calls, 1)

    async def test_payload_validation_error_is_not_retried(self) -> None:
        provider = _SequencedProvider([{}])

        with self.assertRaises(PayloadValidationError):
            await _agent(provider).run(
                _Payload(answer="input"),
                system_prompt="test",
                max_technical_retries=5,
                timeout_seconds=1,
            )

        self.assertEqual(provider.calls, 1)

    async def test_non_retryable_error_is_not_retried(self) -> None:
        provider = _SequencedProvider([NonRetryableAgentError("invalid request")])

        with self.assertRaises(NonRetryableAgentError) as raised:
            await _agent(provider).run(
                _Payload(answer="input"),
                system_prompt="test",
                max_technical_retries=5,
                timeout_seconds=1,
            )

        self.assertEqual(raised.exception.retry_count, 0)
        self.assertEqual(provider.calls, 1)

    async def test_timeout_retries_once(self) -> None:
        provider = _SequencedProvider([TimeoutError("temporary timeout")])

        with self.assertRaises(RetryableAgentError) as raised:
            await _agent(provider).run(
                _Payload(answer="input"),
                system_prompt="test",
                max_technical_retries=5,
                timeout_seconds=1,
            )

        self.assertEqual(raised.exception.retry_count, 1)
        self.assertEqual(provider.calls, 2)

    async def test_unclassified_agent_error_fails_without_retry(self) -> None:
        provider = _SequencedProvider([AgentExecutionError("unknown provider error")])

        with self.assertRaises(NonRetryableAgentError):
            await _agent(provider).run(
                _Payload(answer="input"),
                system_prompt="test",
                max_technical_retries=5,
                timeout_seconds=1,
            )

        self.assertEqual(provider.calls, 1)

    def test_error_message_records_retry_context_and_redacts_secrets(self) -> None:
        provider = _SequencedProvider([{"answer": "unused"}])
        agent = _agent(provider)
        request = PMPMessage.create(
            sender_agent_id="researcher.manager",
            receiver_agent_id=agent.agent_id,
            message_type=MessageType.TASK,
            objective="test error logging",
            payload={"task_id": "task_test"},
        )
        root_error = TimeoutError("Authorization: Bearer secret-token")
        error = RetryableAgentError(
            "OPENROUTER_API_KEY=secret-value temporary failure",
            http_status=429,
            retry_count=1,
            provider="openrouter",
            model_id="test/model",
        )
        error.__cause__ = root_error

        message = agent.create_error_message(request, error)

        serialized_payload = json.dumps(message.payload)
        self.assertEqual(message.metadata.retry_count, 1)
        self.assertEqual(message.payload["retry_count"], 1)
        self.assertEqual(message.payload["http_status"], 429)
        self.assertEqual(message.payload["task_id"], "task_test")
        self.assertNotIn("secret-token", serialized_payload)
        self.assertNotIn("secret-value", serialized_payload)


if __name__ == "__main__":
    unittest.main()
