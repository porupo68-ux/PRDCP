from __future__ import annotations

import asyncio
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel

from common.models.errors import AgentExecutionError


class OpenRouterModelProvider:
    def __init__(self, *, api_key: str, base_url: str = "https://openrouter.ai/api/v1", timeout: int = 180) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[BaseModel],
    ) -> dict:
        if not model:
            raise AgentExecutionError("OpenRouter model ID is not configured for this agent")
        schema = output_schema.model_json_schema()
        body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                    + "\n必ずJSONオブジェクトだけを返してください。出力JSON Schema:\n"
                    + json.dumps(schema, ensure_ascii=False),
                },
                {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},
        }
        return await asyncio.to_thread(self._post, body)

    def _post(self, body: dict) -> dict:
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AgentExecutionError(f"OpenRouter HTTP {exc.code}: {detail[:500]}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AgentExecutionError(f"OpenRouter request failed: {exc}") from exc
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise AgentExecutionError("OpenRouter did not return a valid JSON object") from exc

