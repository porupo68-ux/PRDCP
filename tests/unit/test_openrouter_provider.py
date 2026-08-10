from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from pydantic import BaseModel

from common.models.errors import AgentExecutionError
from providers.openrouter_provider import OpenRouterModelProvider


class _Payload(BaseModel):
    answer: str


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
            )

        self.assertEqual(result, {"answer": "ok"})
        request = post.call_args.args[0]
        sent = json.loads(request.data.decode("utf-8"))
        self.assertEqual(sent["model"], "test/model")
        self.assertEqual(sent["response_format"], {"type": "json_object"})

    async def test_missing_model_is_rejected_before_network_access(self) -> None:
        provider = OpenRouterModelProvider(api_key="test-key")

        with patch("providers.openrouter_provider.urlopen") as post:
            with self.assertRaisesRegex(AgentExecutionError, "model ID"):
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
            with self.assertRaisesRegex(AgentExecutionError, "valid JSON object"):
                await provider.generate_structured(
                    model="test/model",
                    system_prompt="system",
                    input_data={},
                    output_schema=_Payload,
                )


if __name__ == "__main__":
    unittest.main()
