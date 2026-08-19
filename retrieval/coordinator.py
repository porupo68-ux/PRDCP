from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from common.models.errors import NonRetryableAgentError
from common.provider_capability_repair import PROVIDER_CAPABILITY_REPAIR_SUFFIX
from common.provider_contract_repair import PROVIDER_CONTRACT_REPAIR_SUFFIX
from common.provider_output_repair import PROVIDER_OUTPUT_REPAIR_SUFFIX
from common.provider_retry import OPERATOR_RETRY_SUFFIX
from common.provider_runtime_model_repair import RUNTIME_MODEL_REPAIR_SUFFIX
from retrieval.models import RetrievedContext, RetrievedSource, RetrievalStrategy
from retrieval.providers import RetrievalProvider


class RetrievalCoordinator:
    def __init__(
        self,
        provider: RetrievalProvider,
        *,
        data_dir: Path,
        demo_safe_mode: bool,
    ) -> None:
        self.provider = provider
        self.data_dir = Path(data_dir)
        self.demo_safe_mode = demo_safe_mode

    async def prepare(
        self,
        *,
        workflow_id: str,
        task_id: str,
        research_question_id: str | None,
        agent_id: str,
        strategy: RetrievalStrategy,
        queries: list[str],
        max_results: int,
        timeout_seconds: int,
        before_provider_call: Callable[[Path, str], None] | None = None,
    ) -> RetrievedContext:
        canonical_task_id = self._canonical_task_id(task_id)
        retrieval_id = self._retrieval_id(
            workflow_id, canonical_task_id, agent_id, strategy.value
        )
        context_path = self._context_path(workflow_id, retrieval_id)
        if context_path.exists():
            return RetrievedContext.model_validate_json(
                context_path.read_text(encoding="utf-8")
            )

        reservation_path = self._reservation_path(workflow_id, retrieval_id)
        reservation = {
            "retrieval_id": retrieval_id,
            "workflow_id": workflow_id,
            "task_id": canonical_task_id,
            "agent_id": agent_id,
            "strategy": strategy.value,
            "provider_id": self.provider.provider_id,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            reservation_path.parent.mkdir(parents=True, exist_ok=True)
            with reservation_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(reservation, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise NonRetryableAgentError(
                "RETRIEVAL_AMBIGUOUS_STATE: reservation exists without persisted context; "
                "automatic search retry is blocked",
                provider=self.provider.provider_id,
                automatic_retry_allowed=False,
            ) from exc
        except OSError as exc:
            raise NonRetryableAgentError(
                "RETRIEVAL_RESERVATION_ERROR: search call blocked before provider invocation",
                provider=self.provider.provider_id,
                automatic_retry_allowed=False,
            ) from exc

        # One-shot recovery workflows consume their durable authorization only
        # after this reservation exists, but still before a paid search starts.
        if before_provider_call is not None:
            before_provider_call(reservation_path, retrieval_id)

        all_results = []
        for query in queries:
            results = await self.provider.search(
                query=query,
                strategy=strategy,
                max_results=max_results,
                timeout_seconds=timeout_seconds,
            )
            all_results.extend((query, result) for result in results)
            if len(all_results) >= max_results:
                break
        now = datetime.now(timezone.utc)
        seen_urls: set[str] = set()
        sources: list[RetrievedSource] = []
        for query, result in all_results:
            normalized_url = str(result.url)
            if normalized_url in seen_urls:
                continue
            source_hash = hashlib.sha256(
                f"{retrieval_id}\0{normalized_url}".encode("utf-8")
            ).hexdigest()[:24]
            sources.append(
                RetrievedSource(
                    source_id=f"source_{source_hash}",
                    url=result.url,
                    title=result.title,
                    content=result.content,
                    rank=len(sources) + 1,
                    query=query,
                    provider_id=self.provider.provider_id,
                    retrieved_at=now,
                )
            )
            seen_urls.add(normalized_url)
            if len(sources) >= max_results:
                break
        context = RetrievedContext(
            retrieval_id=retrieval_id,
            workflow_id=workflow_id,
            task_id=canonical_task_id,
            research_question_id=research_question_id,
            agent_id=agent_id,
            retrieval_strategy=strategy,
            queries=queries,
            sources=sources,
            limitations=[] if sources else ["RETRIEVAL_NO_RESULTS"],
            retrieved_at=now,
        )
        try:
            context_path.parent.mkdir(parents=True, exist_ok=True)
            with context_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(context.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise NonRetryableAgentError(
                "RETRIEVAL_PERSISTENCE_ERROR: provider returned results but context could not "
                "be durably saved; automatic search retry is blocked",
                provider=self.provider.provider_id,
                automatic_retry_allowed=False,
            ) from exc
        return context

    def _context_path(self, workflow_id: str, retrieval_id: str) -> Path:
        return self.data_dir / "retrieval_contexts" / self._component(workflow_id) / f"{retrieval_id}.json"

    def _reservation_path(self, workflow_id: str, retrieval_id: str) -> Path:
        root = self.provider.reservation_root or self.data_dir / "retrieval_call_reservations"
        return Path(root) / self.provider.provider_id / self._component(workflow_id) / f"{retrieval_id}.json"

    @staticmethod
    def _retrieval_id(workflow_id: str, task_id: str, agent_id: str, strategy: str) -> str:
        digest = hashlib.sha256(
            f"{workflow_id}\0{task_id}\0{agent_id}\0{strategy}".encode("utf-8")
        ).hexdigest()[:24]
        return f"retrieval_{digest}"

    @staticmethod
    def _canonical_task_id(task_id: str) -> str:
        for suffix in (
            PROVIDER_CAPABILITY_REPAIR_SUFFIX,
            PROVIDER_CONTRACT_REPAIR_SUFFIX,
            PROVIDER_OUTPUT_REPAIR_SUFFIX,
            RUNTIME_MODEL_REPAIR_SUFFIX,
            OPERATOR_RETRY_SUFFIX,
        ):
            if task_id.endswith(suffix):
                return task_id[: -len(suffix)]
        return task_id

    @staticmethod
    def _component(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            return value
        return "id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()
