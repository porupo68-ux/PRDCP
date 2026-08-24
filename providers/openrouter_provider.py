from __future__ import annotations

import asyncio
import hashlib
import json
import math
import socket
from http.client import IncompleteRead
from math import ceil
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel

from common.models.errors import (
    NonRetryableAgentError,
    ProviderCapabilityError,
    ProviderResponseContractError,
    RetryableAgentError,
)
from common.provider_schema_compatibility import (
    specialize_provider_output_schema,
    validate_provider_schema_compatibility,
)
from common.structured_outputs import (
    strict_output_schema,
    validate_strict_output_schema,
)
from providers.openrouter_capabilities import OpenRouterModelCapabilityClient
from providers.openrouter_batch import OpenRouterBatchClient, is_openrouter_batch_model


RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 600
MODEL_INPUT_CONTEXT_LIMITS = {
    # OpenRouter's DeepSeek R1 endpoint returned this exact limit during the
    # saved Deliberation recovery. Unknown models are not guessed locally.
    "deepseek/deepseek-r1": 64_000,
}
CONTEXT_INPUT_SAFETY_RATIO = 0.90


def estimate_openrouter_input_tokens(value: str) -> int:
    """Conservative dependency-free estimate for mixed Japanese/ASCII prompts."""

    units = sum(1.0 if ord(character) > 127 else 0.25 for character in value)
    return ceil(units)


class OpenRouterModelProvider:
    provider_id = "openrouter"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        reservation_root: Path | None = None,
        capability_client: OpenRouterModelCapabilityClient | None = None,
        batch_poll_interval_seconds: float = 5.0,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required")
        if timeout <= 0:
            raise ValueError("OpenRouter timeout must be positive")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.reservation_root = reservation_root
        self.capability_client = capability_client or OpenRouterModelCapabilityClient(
            base_url=self.base_url,
        )
        self.batch_client = OpenRouterBatchClient(
            api_key=api_key,
            base_url=self.base_url,
            default_timeout_seconds=timeout,
            poll_interval_seconds=batch_poll_interval_seconds,
        )

    def can_resume_invocation(
        self,
        *,
        reservation_path: Path,
        model_id: str,
    ) -> bool:
        return is_openrouter_batch_model(model_id) and (
            OpenRouterBatchClient.has_resumable_state(reservation_path)
        )

    def can_retry_failed_invocation(
        self,
        *,
        reservation_path: Path,
        model_id: str,
    ) -> bool:
        return is_openrouter_batch_model(model_id) and (
            OpenRouterBatchClient.has_terminal_failed_state(
                reservation_path,
                model_id=model_id,
            )
        )

    async def generate_structured(
        self,
        *,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[BaseModel],
        timeout_seconds: int | None = None,
        invocation_reservation_path: Path | None = None,
    ) -> dict:
        if not model:
            raise NonRetryableAgentError(
                "OpenRouter model ID is not configured for this agent",
                provider="openrouter",
            )
        self.validate_request_budget(
            model=model,
            system_prompt=system_prompt,
            input_data=input_data,
            output_schema=output_schema,
        )
        body = self.build_request_body(
            model=model,
            system_prompt=system_prompt,
            input_data=input_data,
            output_schema=output_schema,
        )
        if is_openrouter_batch_model(model):
            return await asyncio.to_thread(
                self._batch_post,
                body,
                timeout_seconds,
                invocation_reservation_path,
            )
        return await asyncio.to_thread(self._post, body, timeout_seconds)

    def _batch_post(
        self,
        body: dict,
        timeout_seconds: int | None,
        invocation_reservation_path: Path | None,
    ) -> dict:
        model_id = str(body.get("model") or "")
        envelope = self.batch_client.execute_chat(
            model_id=model_id,
            request_body=body,
            reservation_path=invocation_reservation_path,
            timeout_seconds=timeout_seconds,
        )
        return self._extract_structured_content(envelope, model_id=model_id)

    @staticmethod
    def build_request_body(
        *,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[BaseModel],
    ) -> dict:
        """Build the exact secret-free JSON body posted to OpenRouter."""

        schema = strict_output_schema(output_schema, input_data=input_data)
        schema = specialize_provider_output_schema(model, schema)
        validate_strict_output_schema(schema, schema_name=output_schema.__name__)
        validate_provider_schema_compatibility(model, schema)
        return {
            "model": model,
            # OpenRouter otherwise defaults this to false and may route a
            # response_format request to an endpoint that ignores parameters it
            # does not implement. Every PRDCP call is a Structured Output call,
            # so parameter support is a hard routing requirement.
            "provider": {"require_parameters": True},
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

    def validate_request_budget(
        self,
        *,
        agent_id: str | None = None,
        model: str,
        system_prompt: str,
        input_data: dict,
        output_schema: type[BaseModel],
    ) -> int:
        """Reject a known-overflow request locally before HTTP is attempted."""

        try:
            self.capability_client.require_compatible(model)
        except ProviderCapabilityError as exc:
            if not agent_id:
                raise
            raise ProviderCapabilityError(
                f"MODEL_CAPABILITY_ERROR: agent={agent_id}; model={model}; "
                "required=response_format=json_schema, json_schema.strict=true, "
                "provider.require_parameters=true; "
                f"reason={exc}. No paid chat completion request was sent.",
                provider="openrouter",
                model_id=model,
            ) from exc
        context_limit = MODEL_INPUT_CONTEXT_LIMITS.get(model.lower())
        schema = strict_output_schema(output_schema, input_data=input_data)
        schema = specialize_provider_output_schema(model, schema)
        validate_strict_output_schema(schema, schema_name=output_schema.__name__)
        validate_provider_schema_compatibility(model, schema)
        serialized = "\n".join(
            (
                system_prompt,
                json.dumps(input_data, ensure_ascii=False, separators=(",", ":")),
                json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            )
        )
        estimated_tokens = estimate_openrouter_input_tokens(serialized)
        if context_limit is None:
            return estimated_tokens
        safe_budget = int(context_limit * CONTEXT_INPUT_SAFETY_RATIO)
        if estimated_tokens > safe_budget:
            raise NonRetryableAgentError(
                "OpenRouter local context budget exceeded before provider request: "
                f"estimated_input_tokens={estimated_tokens}, safe_budget={safe_budget}, "
                f"model_context_limit={context_limit}. No HTTP request was sent.",
                provider="openrouter",
                model_id=model,
            )
        return estimated_tokens

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
                response_text = response.read().decode("utf-8")
                result = self._decode_strict_json_object(
                    response_text,
                    model_id=str(body.get("model") or "") or None,
                    document_name="response envelope",
                )
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            safe_detail = self._safe_detail(detail)
            if (
                exc.code == 404
                and "no endpoints found that can handle the requested parameters"
                in safe_detail.lower()
            ):
                raise ProviderCapabilityError(
                    f"OpenRouter HTTP {exc.code}: {safe_detail}",
                    http_status=exc.code,
                    provider="openrouter",
                    model_id=str(body.get("model") or "") or None,
                ) from exc
            error_type = (
                RetryableAgentError
                if exc.code in RETRYABLE_HTTP_STATUSES
                else NonRetryableAgentError
            )
            raise error_type(
                f"OpenRouter HTTP {exc.code}: {safe_detail}",
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
        except IncompleteRead as exc:
            # The provider may have completed and billed the generation even
            # though the response body was truncated in transit.  Classify it
            # as recoverable, but require a persisted operator authorization
            # instead of automatically duplicating the logical task.
            raise RetryableAgentError(
                "OpenRouter response body was interrupted before completion",
                provider="openrouter",
                model_id=str(body.get("model") or "") or None,
                automatic_retry_allowed=False,
            ) from exc
        except UnicodeDecodeError as exc:
            raise NonRetryableAgentError(
                "OpenRouter returned an invalid JSON response body",
                provider="openrouter",
                model_id=str(body.get("model") or "") or None,
            ) from exc
        return self._extract_structured_content(
            result,
            model_id=str(body.get("model") or "") or None,
        )

    def _extract_structured_content(
        self,
        result: dict,
        *,
        model_id: str | None,
    ) -> dict:
        try:
            content = result["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
            if not isinstance(content, str):
                raise TypeError("message content must be text")
            return self._decode_strict_json_object(
                content,
                model_id=model_id,
                document_name="structured message content",
            )
        except ProviderResponseContractError:
            raise
        except (KeyError, IndexError, TypeError) as exc:
            raise NonRetryableAgentError(
                "OpenRouter did not return a valid JSON object",
                provider="openrouter",
                model_id=model_id,
            ) from exc

    @classmethod
    def _decode_strict_json_object(
        cls,
        content: str,
        *,
        model_id: str | None,
        document_name: str,
    ) -> dict:
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite numeric constant {value}")

        try:
            parsed = json.loads(content, parse_constant=reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ProviderResponseContractError(
                f"OpenRouter {document_name} violated the strict JSON contract: {exc}",
                provider="openrouter",
                model_id=model_id,
                response_content_sha256=content_hash,
                response_content_length=len(content),
                response_invalid_path="$",
            ) from exc

        invalid_path = cls._first_non_finite_path(parsed)
        if invalid_path is not None:
            raise ProviderResponseContractError(
                f"OpenRouter {document_name} contains a non-finite number at {invalid_path}",
                provider="openrouter",
                model_id=model_id,
                response_content_sha256=content_hash,
                response_content_length=len(content),
                response_root_type=type(parsed).__name__,
                response_invalid_path=invalid_path,
            )
        if not isinstance(parsed, dict):
            raise ProviderResponseContractError(
                f"OpenRouter {document_name} root must be a JSON object, got "
                f"{type(parsed).__name__}",
                provider="openrouter",
                model_id=model_id,
                response_content_sha256=content_hash,
                response_content_length=len(content),
                response_root_type=type(parsed).__name__,
                response_invalid_path="$",
            )
        return parsed

    @classmethod
    def _first_non_finite_path(cls, value: object, path: str = "$") -> str | None:
        if isinstance(value, float) and not math.isfinite(value):
            return path
        if isinstance(value, dict):
            for key, item in value.items():
                found = cls._first_non_finite_path(item, f"{path}/{key}")
                if found is not None:
                    return found
        elif isinstance(value, list):
            for index, item in enumerate(value):
                found = cls._first_non_finite_path(item, f"{path}/{index}")
                if found is not None:
                    return found
        return None

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
