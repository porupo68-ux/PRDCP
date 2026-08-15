from __future__ import annotations

import json
import unittest
from enum import Enum
from io import BytesIO
from typing import Any
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from pydantic import BaseModel, Field

from common.models.errors import (
    AgentExecutionError,
    NonRetryableAgentError,
    RetryableAgentError,
)
from common.structured_outputs import (
    StrictStructuredOutputSchemaError,
    strict_output_schema,
)
from providers.openrouter_provider import OpenRouterModelProvider


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
