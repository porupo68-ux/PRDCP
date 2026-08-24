from __future__ import annotations

import json
import tempfile
import unittest
from enum import Enum
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from pydantic import BaseModel, Field

from common.models.errors import (
    AgentExecutionError,
    NonRetryableAgentError,
    ProviderCapabilityError,
    ProviderResponseContractError,
    RetryableAgentError,
)
from common.structured_outputs import (
    StrictStructuredOutputSchemaError,
    strict_output_schema,
)
from providers.openrouter_provider import (
    OpenRouterModelProvider,
    estimate_openrouter_input_tokens,
)
from providers.openrouter_batch import OpenRouterBatchClient
from providers.openrouter_capabilities import OpenRouterModelCapabilityClient
from researcher.schemas.research_result import ResearchResult
from tests.researcher_helpers import valid_source


class _Payload(BaseModel):
    answer: str


class _NestedDefaults(BaseModel):
    affected_agent_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class _PayloadWithDefaults(BaseModel):
    findings: list[_NestedDefaults] = Field(default_factory=list)
    revision_scope: str = "none"
    optional_note: str | None = None


class _Scope(str, Enum):
    NONE = "none"


class _PayloadWithRefDefault(BaseModel):
    revision_scope: _Scope = _Scope.NONE


class _InvalidFreeObjectPayload(BaseModel):
    payload: dict[str, Any]


class _Response:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class OpenRouterProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.capability_patch = patch.object(
            OpenRouterModelCapabilityClient,
            "require_compatible",
            return_value=None,
        )
        self.capability_patch.start()
        self.addCleanup(self.capability_patch.stop)

    async def test_incompatible_model_is_rejected_before_paid_post(self) -> None:
        class RejectingCapabilityClient:
            def require_compatible(self, model_id: str):
                raise ProviderCapabilityError(
                    f"MODEL_CAPABILITY_ERROR: model={model_id}; Provider call = 0",
                    provider="openrouter",
                    model_id=model_id,
                )

        provider = OpenRouterModelProvider(
            api_key="test-key",
            timeout=1,
            capability_client=RejectingCapabilityClient(),
        )
        with patch.object(provider, "_post") as post:
            with self.assertRaises(ProviderCapabilityError):
                await provider.generate_structured(
                    model="unsupported/model",
                    system_prompt="system",
                    input_data={"topic": "test"},
                    output_schema=_Payload,
                )
        post.assert_not_called()

    async def test_batch_model_uses_batch_endpoint_and_polls_correlated_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reservation = (
                Path(temporary)
                / "provider_call_reservations"
                / "openrouter"
                / "workflow_1"
                / "task_1.json"
            )
            reservation.parent.mkdir(parents=True)
            reservation.write_text("{}", encoding="utf-8")
            provider = OpenRouterModelProvider(
                api_key="test-key",
                timeout=10,
                batch_poll_interval_seconds=0,
            )
            completed_body = {
                "id": "gen-batch-1",
                "choices": [
                    {"message": {"content": json.dumps({"answer": "ok"})}}
                ],
            }
            responses = [
                _Response({"id": "0198f00d-1234-7abc-8def-123456789abc", "status": "validating"}),
                HTTPError(
                    "https://openrouter.ai/api/beta/batches/id",
                    404,
                    "not replicated yet",
                    {},
                    BytesIO(b'{"error":{"message":"Batch job not found"}}'),
                ),
                _Response(
                    {
                        "id": "0198f00d-1234-7abc-8def-123456789abc",
                        "status": "completed",
                        "request_counts": {"total": 1, "completed": 1, "failed": 0},
                        "usage": {"total_tokens": 12, "cost": 0.001},
                        "results": [
                            {
                                "custom_id": None,
                                "response": {"status_code": 200, "body": completed_body},
                                "error": None,
                            }
                        ],
                    }
                ),
            ]
            captured = []

            def fake_urlopen(request, timeout):
                captured.append((request, timeout))
                if len(captured) == 1:
                    submitted = json.loads(request.data.decode("utf-8"))
                    responses[2]._body = json.dumps(
                        {
                            **json.loads(responses[2]._body.decode("utf-8")),
                            "results": [
                                {
                                    "custom_id": submitted["requests"][0]["custom_id"],
                                    "response": {
                                        "status_code": 200,
                                        "body": completed_body,
                                    },
                                    "error": None,
                                }
                            ],
                        }
                    ).encode("utf-8")
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

            with patch("providers.openrouter_batch.urlopen", side_effect=fake_urlopen):
                result = await provider.generate_structured(
                    model="google/gemini-3.7-flash:batch",
                    system_prompt="system",
                    input_data={"topic": "test"},
                    output_schema=_Payload,
                    invocation_reservation_path=reservation,
                )

            self.assertEqual(result, {"answer": "ok"})
            self.assertEqual(len(captured), 3)
            self.assertEqual(captured[0][0].full_url, "https://openrouter.ai/api/beta/batches")
            submitted = json.loads(captured[0][0].data.decode("utf-8"))
            self.assertEqual(list(submitted), ["endpoint", "model", "requests"])
            self.assertEqual(submitted["model"], "google/gemini-3.7-flash")
            request_body = submitted["requests"][0]["body"]
            self.assertEqual(request_body["model"], "google/gemini-3.7-flash")
            self.assertNotIn("provider", request_body)
            self.assertIn("response_format", request_body)
            state_path = OpenRouterBatchClient.state_path(
                reservation,
                invocation_discriminator="structured-output",
            )
            state_text = state_path.read_text(encoding="utf-8")
            self.assertNotIn("system", state_text)
            self.assertNotIn("test-key", state_text)
            self.assertIn(
                '"batch_id": "0198f00d-1234-7abc-8def-123456789abc"',
                state_text,
            )
            self.assertIn('"poll_failure_count": 1', state_text)

    async def test_batch_recovery_polls_saved_id_without_duplicate_submission(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reservation = Path(temporary) / "provider_call_reservations" / "openrouter" / "w" / "t.json"
            reservation.parent.mkdir(parents=True)
            reservation.write_text("{}", encoding="utf-8")
            provider = OpenRouterModelProvider(
                api_key="test-key", timeout=10, batch_poll_interval_seconds=0
            )
            submitted_custom_id = None

            def first_urlopen(request, timeout):
                nonlocal submitted_custom_id
                if request.method == "POST":
                    payload = json.loads(request.data.decode("utf-8"))
                    submitted_custom_id = payload["requests"][0]["custom_id"]
                    return _Response({"id": "batch_saved", "status": "validating"})
                return _Response(
                    {
                        "id": "batch_saved",
                        "status": "completed",
                        "results": [
                            {
                                "custom_id": submitted_custom_id,
                                "response": {
                                    "status_code": 200,
                                    "body": {
                                        "id": "gen-1",
                                        "choices": [
                                            {"message": {"content": '{"answer":"ok"}'}}
                                        ],
                                    },
                                },
                                "error": None,
                            }
                        ],
                    }
                )

            kwargs = {
                "model": "google/gemini-3.7-flash:batch",
                "system_prompt": "system",
                "input_data": {"topic": "test"},
                "output_schema": _Payload,
                "invocation_reservation_path": reservation,
            }
            with patch("providers.openrouter_batch.urlopen", side_effect=first_urlopen):
                self.assertEqual(await provider.generate_structured(**kwargs), {"answer": "ok"})

            second_requests = []

            def second_urlopen(request, timeout):
                second_requests.append(request)
                return first_urlopen(request, timeout)

            with patch("providers.openrouter_batch.urlopen", side_effect=second_urlopen):
                self.assertEqual(await provider.generate_structured(**kwargs), {"answer": "ok"})
            self.assertEqual(len(second_requests), 1)
            self.assertEqual(second_requests[0].method, "GET")

    def test_only_correlated_terminal_batch_state_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reservation = (
                Path(temporary)
                / "provider_call_reservations"
                / "openrouter"
                / "workflow"
                / "task.json"
            )
            reservation.parent.mkdir(parents=True)
            reservation.write_text("{}", encoding="utf-8")
            state_path = OpenRouterBatchClient.state_path(
                reservation,
                invocation_discriminator="structured-output",
            )
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "batch_id": "batch-failed",
                        "status": "failed",
                        "model_id": "google/gemini-3.7-flash:batch",
                    }
                ),
                encoding="utf-8",
            )
            provider = OpenRouterModelProvider(api_key="test-key")

            self.assertTrue(
                provider.can_retry_failed_invocation(
                    reservation_path=reservation,
                    model_id="google/gemini-3.7-flash:batch",
                )
            )
            self.assertFalse(
                provider.can_retry_failed_invocation(
                    reservation_path=reservation,
                    model_id="google/gemini-3.7-pro:batch",
                )
            )

    async def test_ambiguous_batch_submission_cannot_automatically_resubmit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reservation = Path(temporary) / "provider_call_reservations" / "openrouter" / "w" / "t.json"
            reservation.parent.mkdir(parents=True)
            reservation.write_text("{}", encoding="utf-8")
            provider = OpenRouterModelProvider(
                api_key="test-key", timeout=10, batch_poll_interval_seconds=0
            )
            kwargs = {
                "model": "google/gemini-3.7-flash:batch",
                "system_prompt": "system",
                "input_data": {"topic": "test"},
                "output_schema": _Payload,
                "invocation_reservation_path": reservation,
            }
            with patch(
                "providers.openrouter_batch.urlopen",
                side_effect=URLError(TimeoutError("timed out")),
            ) as post:
                with self.assertRaisesRegex(
                    NonRetryableAgentError, "SUBMISSION_AMBIGUOUS"
                ):
                    await provider.generate_structured(**kwargs)
            self.assertEqual(post.call_count, 1)
            with patch("providers.openrouter_batch.urlopen") as post:
                with self.assertRaisesRegex(
                    NonRetryableAgentError, "SUBMISSION_AMBIGUOUS"
                ):
                    await provider.generate_structured(**kwargs)
            post.assert_not_called()

    def test_agent_preflight_error_identifies_effective_agent_and_model(self) -> None:
        class RejectingCapabilityClient:
            def require_compatible(self, model_id: str):
                raise ProviderCapabilityError(
                    "MODEL_CAPABILITY_ERROR: reason=NO_ENDPOINTS",
                    provider="openrouter",
                    model_id=model_id,
                )

        provider = OpenRouterModelProvider(
            api_key="test-key",
            timeout=1,
            capability_client=RejectingCapabilityClient(),
        )
        with self.assertRaises(ProviderCapabilityError) as raised:
            provider.validate_request_budget(
                agent_id="producer.topic_scout",
                model="unsupported/model",
                system_prompt="system",
                input_data={"topic": "test"},
                output_schema=_Payload,
            )

        message = str(raised.exception)
        self.assertIn("agent=producer.topic_scout", message)
        self.assertIn("model=unsupported/model", message)
        self.assertIn("No paid chat completion request was sent", message)

    async def test_known_context_overflow_is_rejected_before_network_access(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)

        with patch("providers.openrouter_provider.urlopen") as post:
            with self.assertRaisesRegex(
                NonRetryableAgentError,
                "local context budget exceeded",
            ):
                await provider.generate_structured(
                    model="deepseek/deepseek-r1",
                    system_prompt="あ" * 60_000,
                    input_data={"topic": "fault injection"},
                    output_schema=_Payload,
                )

        post.assert_not_called()

    def test_mixed_language_token_estimator_is_conservative(self) -> None:
        self.assertEqual(estimate_openrouter_input_tokens("あ" * 100), 100)
        self.assertEqual(estimate_openrouter_input_tokens("a" * 100), 25)

    async def test_research_result_schema_is_narrowed_before_network_send(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        payload = {
            "task_id": "task_academic",
            "research_question_id": "rq_employment",
            "agent_id": "researcher.academic_researcher",
            "sources": [valid_source("ACADEMIC")],
            "search_summary": "done",
            "coverage_status": "COMPLETE",
            "limitations": [],
        }
        response = _Response(
            {"choices": [{"message": {"content": json.dumps(payload)}}]}
        )
        input_data = {
            "target_agent_id": "researcher.academic_researcher",
            "research_target": "ACADEMIC",
        }

        with patch("providers.openrouter_provider.urlopen", return_value=response) as post:
            await provider.generate_structured(
                model="test/model",
                system_prompt="system",
                input_data=input_data,
                output_schema=ResearchResult,
            )

        request = post.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        schema = sent["response_format"]["json_schema"]["schema"]
        self.assertEqual(
            schema["properties"]["agent_id"]["enum"],
            ["researcher.academic_researcher"],
        )
        self.assertEqual(
            schema["$defs"]["ResearchSource"]["properties"]["source_type"][
                "enum"
            ],
            ["ACADEMIC"],
        )

    async def test_structured_response_is_parsed_without_network_access(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        response = _Response(
            {
                "choices": [
                    {"message": {"content": json.dumps({"answer": "ok"})}},
                ]
            }
        )

        with patch("providers.openrouter_provider.urlopen", return_value=response) as post:
            result = await provider.generate_structured(
                model="test/model",
                system_prompt="system",
                input_data={"topic": "test"},
                output_schema=_Payload,
                timeout_seconds=900,
            )

        self.assertEqual(result, {"answer": "ok"})
        request = post.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["model"], "test/model")
        self.assertEqual(sent["provider"], {"require_parameters": True})
        self.assertNotIn("plugins", sent)
        response_format = sent["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(response_format["json_schema"]["name"], "_Payload")
        self.assertTrue(response_format["json_schema"]["strict"])
        self.assertEqual(
            response_format["json_schema"]["schema"],
            strict_output_schema(_Payload),
        )
        self.assertEqual(post.call_args.kwargs["timeout"], 900)

    async def test_defaulted_fields_are_required_recursively_in_sent_schema(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        response = _Response(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "findings": [],
                                    "revision_scope": "none",
                                    "optional_note": None,
                                }
                            )
                        }
                    }
                ]
            }
        )

        with patch("providers.openrouter_provider.urlopen", return_value=response) as post:
            await provider.generate_structured(
                model="test/model",
                system_prompt="system",
                input_data={},
                output_schema=_PayloadWithDefaults,
            )

        request = post.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        schema = sent["response_format"]["json_schema"]["schema"]
        self.assertEqual(schema["required"], list(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        nested = schema["$defs"]["_NestedDefaults"]
        self.assertEqual(nested["required"], list(nested["properties"]))
        self.assertFalse(nested["additionalProperties"])
        self.assertNotIn("default", schema["properties"]["revision_scope"])
        self.assertNotIn("default", schema["properties"]["optional_note"])

    async def test_ref_default_is_removed_before_the_request_is_sent(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        response = _Response(
            {
                "choices": [
                    {"message": {"content": json.dumps({"revision_scope": "none"})}}
                ]
            }
        )

        with patch("providers.openrouter_provider.urlopen", return_value=response) as post:
            await provider.generate_structured(
                model="test/model",
                system_prompt="system",
                input_data={},
                output_schema=_PayloadWithRefDefault,
            )

        request = post.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        revision_scope = sent["response_format"]["json_schema"]["schema"][
            "properties"
        ]["revision_scope"]
        self.assertEqual(revision_scope, {"$ref": "#/$defs/_Scope"})
        self.assertEqual(
            _PayloadWithRefDefault.model_json_schema()["properties"]["revision_scope"][
                "default"
            ],
            "none",
        )

    async def test_invalid_schema_is_rejected_before_network_access(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)

        with patch("providers.openrouter_provider.urlopen") as post:
            with self.assertRaises(StrictStructuredOutputSchemaError):
                await provider.generate_structured(
                    model="test/model",
                    system_prompt="system",
                    input_data={},
                    output_schema=_InvalidFreeObjectPayload,
                )

        post.assert_not_called()

    async def test_missing_model_is_rejected_before_network_access(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key")

        with patch("providers.openrouter_provider.urlopen") as post:
            with self.assertRaisesRegex(NonRetryableAgentError, "model ID"):
                await provider.generate_structured(
                    model="",
                    system_prompt="system",
                    input_data={},
                    output_schema=_Payload,
                )

        post.assert_not_called()

    async def test_malformed_provider_response_has_an_actionable_error(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        response = _Response({"choices": []})

        with patch("providers.openrouter_provider.urlopen", return_value=response):
            with self.assertRaisesRegex(NonRetryableAgentError, "valid JSON object"):
                await provider.generate_structured(
                    model="test/model",
                    system_prompt="system",
                    input_data={},
                    output_schema=_Payload,
                )

    async def test_non_finite_structured_root_requires_explicit_operator_retry(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        for content in ("Infinity", "-Infinity", "NaN", "1e999"):
            response = _Response(
                {"choices": [{"message": {"content": content}}]}
            )
            with self.subTest(content=content):
                with patch("providers.openrouter_provider.urlopen", return_value=response):
                    with self.assertRaises(ProviderResponseContractError) as raised:
                        await provider.generate_structured(
                            model="test/model",
                            system_prompt="system",
                            input_data={},
                            output_schema=_Payload,
                        )
                self.assertFalse(raised.exception.automatic_retry_allowed)
                self.assertEqual(raised.exception.response_invalid_path, "$")
                self.assertEqual(raised.exception.response_content_length, len(content))

    async def test_non_object_structured_root_is_rejected_by_provider_boundary(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        for content in ('["not", "an", "object"]', '"scalar"', "null"):
            response = _Response(
                {"choices": [{"message": {"content": content}}]}
            )
            with self.subTest(content=content):
                with patch("providers.openrouter_provider.urlopen", return_value=response):
                    with self.assertRaisesRegex(
                        ProviderResponseContractError,
                        "root must be a JSON object",
                    ):
                        await provider.generate_structured(
                            model="test/model",
                            system_prompt="system",
                            input_data={},
                            output_schema=_Payload,
                        )

    async def test_nested_exponent_overflow_reports_exact_json_path(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        content = '{"answer": "ok", "nested": [{"score": 1e999}]}'
        response = _Response({"choices": [{"message": {"content": content}}]})
        with patch("providers.openrouter_provider.urlopen", return_value=response):
            with self.assertRaises(ProviderResponseContractError) as raised:
                await provider.generate_structured(
                    model="test/model",
                    system_prompt="system",
                    input_data={},
                    output_schema=_Payload,
                )
        self.assertEqual(
            raised.exception.response_invalid_path,
            "$/nested/0/score",
        )

    def test_retryable_http_statuses_are_classified(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                error = HTTPError(
                    "https://example.invalid",
                    status,
                    "temporary error",
                    {},
                    BytesIO(b"temporary provider failure"),
                )
                with patch("providers.openrouter_provider.urlopen", side_effect=error):
                    with self.assertRaises(RetryableAgentError) as raised:
                        provider._post({"model": "test/model"})
                self.assertEqual(raised.exception.http_status, status)

    def test_non_retryable_http_statuses_fail_safely(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        for status in (400, 401, 402, 404, 418):
            with self.subTest(status=status):
                error = HTTPError(
                    "https://example.invalid",
                    status,
                    "request error",
                    {},
                    BytesIO(b"invalid request"),
                )
                with patch("providers.openrouter_provider.urlopen", side_effect=error):
                    with self.assertRaises(NonRetryableAgentError) as raised:
                        provider._post({"model": "test/model"})
                self.assertEqual(raised.exception.http_status, status)

    def test_structured_output_endpoint_404_is_a_capability_failure(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        error = HTTPError(
            "https://example.invalid",
            404,
            "request error",
            {},
            BytesIO(
                b'{"error":{"message":"No endpoints found that can handle '
                b'the requested parameters"}}'
            ),
        )
        with patch("providers.openrouter_provider.urlopen", side_effect=error):
            with self.assertRaises(ProviderCapabilityError) as raised:
                provider._post({"model": "z-ai/glm-4.5-air"})

        self.assertEqual(raised.exception.http_status, 404)
        self.assertEqual(raised.exception.model_id, "z-ai/glm-4.5-air")
        self.assertFalse(raised.exception.automatic_retry_allowed)

    def test_network_error_requires_an_explicit_transient_cause(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        cases = (
            (TimeoutError("timed out"), RetryableAgentError),
            (ConnectionResetError("connection reset"), RetryableAgentError),
            ("unsupported URL scheme", NonRetryableAgentError),
        )
        for reason, expected_error in cases:
            with self.subTest(reason=reason):
                with patch(
                    "providers.openrouter_provider.urlopen",
                    side_effect=URLError(reason),
                ):
                    with self.assertRaises(expected_error):
                        provider._post({"model": "test/model"})

    def test_incomplete_response_requires_operator_retry_without_partial_body_leak(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key", timeout=1)
        interrupted = IncompleteRead(b"partial-secret-body", 10_000)

        with patch(
            "providers.openrouter_provider.urlopen",
            side_effect=interrupted,
        ):
            with self.assertRaises(RetryableAgentError) as raised:
                provider._post({"model": "test/model"})

        self.assertFalse(raised.exception.automatic_retry_allowed)
        self.assertNotIn("partial-secret-body", str(raised.exception))
        self.assertIs(raised.exception.__cause__, interrupted)

    def test_http_error_detail_redacts_api_key(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-secret", timeout=1)
        error = HTTPError(
            "https://example.invalid",
            400,
            "request error",
            {},
            BytesIO(b"request included test-secret"),
        )
        with patch("providers.openrouter_provider.urlopen", side_effect=error):
            with self.assertRaises(AgentExecutionError) as raised:
                provider._post({"model": "test/model"})
        self.assertNotIn("test-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
