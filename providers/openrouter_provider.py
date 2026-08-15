from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel

from common.models.errors import NonRetryableAgentError, RetryableAgentError
from common.structured_outputs import strict_output_schema


RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 600


class OpenRouterModelProvider:
    provider_id = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        reservation_root: Path | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required")
        if timeout <= 0:
            raise ValueError("OpenRouter timeout must be positive")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.reservation_root = reservation_root

    async def generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[BaseModel],
        timeout_seconds: int | None = None,
    ) -> dict:
        if not model:
            raise NonRetryableAgentError(
                "OpenRouter model ID is not configured for this agent",
                provider="openrouter",
            )
        schema = strict_output_schema(output_schema)
        body = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        system_prompt
                        + "\n必ず指定されたJSON Schemaに従うJSONオブジェクトだけを返してください。"
                    ),
                },
                {"role": "user", "content": json.dumps(input_data, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_schema.__name__,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        return await asyncio.to_thread(self._post, body, timeout_seconds)

    def _post(self, body: dict, timeout_seconds: int | None = None) -> dict:
        request_timeout = self.timeout if timeout_seconds is None else timeout_seconds
        if request_timeout <= 0:
            raise ValueError("OpenRouter request timeout must be positive")
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
            with urlopen(request, timeout=request_timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error_type = (
                RetryableAgentError
                if exc.code in RETRYABLE_HTTP_STATUSES
                else NonRetryableAgentError
            )
            raise error_type(
                f"OpenRouter HTTP {exc.code}: {self._safe_detail(detail)}",
                http_status=exc.code,
                provider="openrouter",
                model_id=str(body.get("model") or "") or None,
            ) from exc
        except URLError as exc:
            error_type = (
                RetryableAgentError
                if self._is_retryable_url_error(exc)
                else NonRetryableAgentError
            )
            raise error_type(
                f"OpenRouter network request failed: {exc.reason}",
                provider="openrouter",
                model_id=str(body.get("model") or "") or None,
            ) from exc
        except (TimeoutError, ConnectionError) as exc:
            raise RetryableAgentError(
                f"OpenRouter network request failed: {exc}",
                provider="openrouter",
                model_id=str(body.get("model") or "") or None,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NonRetryableAgentError(
                "OpenRouter returned an invalid JSON response body",
                provider="openrouter",
                model_id=str(body.get("model") or "") or None,
            ) from exc
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            return json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise NonRetryableAgentError(
                "OpenRouter did not return a valid JSON object",
                provider="openrouter",
                model_id=str(body.get("model") or "") or None,
            ) from exc

    def _safe_detail(self, detail: str) -> str:
        return detail.replace(self.api_key, "<redacted>")[:500]

    @staticmethod
    def _is_retryable_url_error(exc: URLError) -> bool:
        reason = exc.reason
        if isinstance(reason, (TimeoutError, ConnectionError)):
            return True
        if isinstance(reason, socket.gaierror):
            return reason.errno == socket.EAI_AGAIN
        message = str(reason).lower()
        return any(
            marker in message
            for marker in (
                "temporarily unavailable",
                "temporary failure in name resolution",
                "timed out",
                "connection reset",
                "connection aborted",
                "connection refused",
            )
        )
