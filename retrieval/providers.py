from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from common.models.errors import NonRetryableAgentError
from providers.openrouter_batch import OpenRouterBatchClient, is_openrouter_batch_model
from retrieval.models import SearchResult, RetrievalStrategy


class RetrievalProvider(Protocol):
    provider_id: str
    reservation_root: Path | None

    async def search(
        self,
        *,
        query: str,
        strategy: RetrievalStrategy,
        max_results: int,
        timeout_seconds: int,
        invocation_reservation_path: Path | None = None,
        invocation_discriminator: str = "retrieval",
    ) -> list[SearchResult]: ...


class MockRetrievalProvider:
    provider_id = "mock"

    def __init__(self, *, reservation_root: Path | None = None) -> None:
        self.reservation_root = reservation_root
        self.calls = 0
        self.failure: Exception | None = None

    async def search(
        self,
        *,
        query: str,
        strategy: RetrievalStrategy,
        max_results: int,
        timeout_seconds: int,
        invocation_reservation_path: Path | None = None,
        invocation_discriminator: str = "retrieval",
    ) -> list[SearchResult]:
        del timeout_seconds, invocation_reservation_path, invocation_discriminator
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        count = min(max_results, 3 if strategy == RetrievalStrategy.GENERAL_OPINION else 1)
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:12]
        return [
            SearchResult(
                url=(
                    f"https://example.invalid/retrieval/{strategy.value.lower()}/"
                    f"{query_hash}/{index}"
                ),
                title=f"Mock {strategy.value} source {query_hash} {index}",
                content=(
                    f"Mock {strategy.value} source {index}. Query: {query}. "
                    "Mock Expert; Mock Research Institute; Mock Academic Journal; "
                    "Mock University; OBSERVATIONAL; "
                    "Mock Statistics Bureau; Japan; Mock News Agency; REPORTING; "
                    "Mock Forum; Mock Politician; Mock Parliament; PARLIAMENT; "
                    "Mock Industry Association; "
                    "INDUSTRY_ASSOCIATION; technology."
                ),
            )
            for index in range(1, count + 1)
        ]


class OpenRouterWebSearchProvider:
    """Retrieval-only OpenRouter call whose citation annotations are persisted.

    This adapter intentionally does not request PRDCP Structured Output. The
    resulting URL/title/excerpt records are the boundary consumed by a later,
    separate StructuredAgent call.
    """

    provider_id = "openrouter_web_search"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        engine: str = "exa",
        reservation_root: Path | None = None,
        batch_poll_interval_seconds: float = 5.0,
    ) -> None:
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is required for retrieval")
        if not model:
            raise ValueError("OPENROUTER_RETRIEVAL_MODEL is required")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.engine = engine
        self.reservation_root = reservation_root
        self.batch_client = OpenRouterBatchClient(
            api_key=api_key,
            base_url=self.base_url,
            default_timeout_seconds=600,
            poll_interval_seconds=batch_poll_interval_seconds,
        )

    async def search(
        self,
        *,
        query: str,
        strategy: RetrievalStrategy,
        max_results: int,
        timeout_seconds: int,
        invocation_reservation_path: Path | None = None,
        invocation_discriminator: str = "retrieval",
    ) -> list[SearchResult]:
        body = {
            "model": self.model,
            "provider": {"require_parameters": True},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Perform the required web search once and answer briefly using only "
                        "the returned sources. Do not invent URLs or source identities."
                    ),
                },
                {
                    "role": "user",
                    "content": f"strategy={strategy.value}\nquery={query}",
                },
            ],
            "tools": [
                {
                    "type": "openrouter:web_search",
                    "parameters": {
                        "engine": self.engine,
                        "max_results": max_results,
                        "max_total_results": max_results,
                    },
                }
            ],
            "tool_choice": "required",
        }
        if is_openrouter_batch_model(self.model):
            envelope = await asyncio.to_thread(
                self.batch_client.execute_chat,
                model_id=self.model,
                request_body=body,
                reservation_path=invocation_reservation_path,
                timeout_seconds=timeout_seconds,
                invocation_discriminator=invocation_discriminator,
            )
            return self._extract(envelope)
        return await asyncio.to_thread(self._post_and_extract, body, timeout_seconds)

    def can_resume(
        self,
        *,
        reservation_path: Path,
        invocation_discriminator: str,
    ) -> bool:
        return is_openrouter_batch_model(self.model) and (
            OpenRouterBatchClient.has_resumable_state(
                reservation_path,
                invocation_discriminator=invocation_discriminator,
            )
        )

    def _post_and_extract(self, body: dict, timeout_seconds: int) -> list[SearchResult]:
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
            with urlopen(request, timeout=timeout_seconds) as response:
                envelope = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").replace(
                self.api_key, "<redacted>"
            )[:500]
            raise NonRetryableAgentError(
                f"RETRIEVAL_PROVIDER_ERROR: OpenRouter HTTP {exc.code}: {detail}",
                http_status=exc.code,
                provider=self.provider_id,
                model_id=self.model,
                automatic_retry_allowed=False,
            ) from exc
        except (URLError, TimeoutError, ConnectionError, socket.timeout) as exc:
            raise NonRetryableAgentError(
                f"RETRIEVAL_TIMEOUT: OpenRouter web search failed: {exc}",
                provider=self.provider_id,
                model_id=self.model,
                automatic_retry_allowed=False,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NonRetryableAgentError(
                "RETRIEVAL_PROVIDER_ERROR: OpenRouter returned invalid JSON",
                provider=self.provider_id,
                model_id=self.model,
                automatic_retry_allowed=False,
            ) from exc

        return self._extract(envelope)

    def _extract(self, envelope: dict) -> list[SearchResult]:
        try:
            annotations = envelope["choices"][0]["message"].get("annotations", [])
        except (KeyError, IndexError, TypeError) as exc:
            raise NonRetryableAgentError(
                "RETRIEVAL_PROVIDER_ERROR: OpenRouter response has no message",
                provider=self.provider_id,
                model_id=self.model,
                automatic_retry_allowed=False,
            ) from exc
        results: list[SearchResult] = []
        seen_urls: set[str] = set()
        for annotation in annotations:
            if not isinstance(annotation, dict) or annotation.get("type") != "url_citation":
                continue
            citation = annotation.get("url_citation", annotation)
            if not isinstance(citation, dict):
                continue
            url = citation.get("url")
            if not isinstance(url, str) or url in seen_urls:
                continue
            title = citation.get("title") or urlsplit(url).hostname or url
            content = citation.get("content") or title
            results.append(SearchResult(url=url, title=str(title), content=str(content)))
            seen_urls.add(url)
        return results
