import hashlib
import json
from copy import deepcopy

from pydantic import BaseModel

from common.models.errors import NonRetryableAgentError
from common.models.pmp import PMPMessage
from common.retrieval_provider_retry import (
    RETRIEVAL_PROVIDER_RETRY_SUFFIX,
    RetrievalProviderRetryAuthorizationStore,
    RetrievalProviderRetryStatus,
)
from producer.agents.base import ProducerAgent
from producer.schemas.general_opinion import GeneralOpinionInput, GeneralOpinionOutput
from retrieval import RetrievalStrategy


def general_opinion_retrieval_plan(
    canonical: GeneralOpinionInput,
) -> tuple[str, str]:
    """Return the canonical Retrieval task and query used by runtime and recovery."""

    revision_hash = hashlib.sha256(
        json.dumps(
            canonical.revision_context,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:8]
    task_id = f"general_opinion_{canonical.selected_topic.topic_id}_{revision_hash}"
    query = (
        f"{canonical.selected_topic.title} 一般的な見解 社会的議論 "
        "代表的な主張 ニュース SNS"
    )
    return task_id, query


class GeneralOpinionAnalyst(ProducerAgent):
    agent_id = "producer.general_opinion_analyst"
    input_schema = GeneralOpinionInput
    output_schema = GeneralOpinionOutput

    async def prepare_provider_input(
        self,
        payload: BaseModel,
        *,
        message: PMPMessage,
        timeout_seconds: int,
    ) -> dict:
        canonical = GeneralOpinionInput.model_validate(payload)
        if self.retrieval_coordinator is None:
            raise NonRetryableAgentError(
                "RETRIEVAL_NOT_CONFIGURED: General Opinion Analyst requires retrieval",
                automatic_retry_allowed=False,
            )
        revision_task_id = message.metadata.extensions.get("retrieval_task_id")
        if revision_task_id is not None and (
            not isinstance(revision_task_id, str) or not revision_task_id.strip()
        ):
            raise NonRetryableAgentError(
                "Invalid retrieval_task_id override for General Opinion Analyst",
                automatic_retry_allowed=False,
            )
        if revision_task_id is None:
            task_id, query = general_opinion_retrieval_plan(canonical)
        else:
            task_id = revision_task_id
            _, query = general_opinion_retrieval_plan(canonical)
        retry_authorization = None
        retry_store = None
        before_provider_call = None
        if task_id.endswith(RETRIEVAL_PROVIDER_RETRY_SUFFIX):
            retrieval_provider = self.retrieval_coordinator.provider
            retrieval_provider_id = getattr(retrieval_provider, "provider_id", None)
            runtime_model_id = getattr(retrieval_provider, "model", None)
            if not isinstance(retrieval_provider_id, str) or not isinstance(
                runtime_model_id, str
            ):
                raise NonRetryableAgentError(
                    "Retrieval provider retry requires stable provider/model identity",
                    automatic_retry_allowed=False,
                )
            retry_store = RetrievalProviderRetryAuthorizationStore(
                self.retrieval_coordinator.data_dir
            )
            retry_authorization = retry_store.require_for_retry_task(
                workflow_id=message.workflow_id,
                retrieval_provider_id=retrieval_provider_id,
                agent_id=self.agent_id,
                retry_task_id=task_id,
                runtime_model_id=runtime_model_id,
            )

            if retry_authorization.status == RetrievalProviderRetryStatus.PENDING.value:
                def consume_before_search(path, actual_retrieval_id):
                    if actual_retrieval_id != retry_authorization.retry_retrieval_id:
                        raise ValueError("Retrieval provider retry identity changed")
                    retry_store.consume(
                        retry_authorization,
                        reservation_path=path,
                    )

                before_provider_call = consume_before_search

        context = await self.retrieval_coordinator.prepare(
            workflow_id=message.workflow_id,
            task_id=task_id,
            research_question_id=None,
            agent_id=self.agent_id,
            strategy=RetrievalStrategy.GENERAL_OPINION,
            queries=[query],
            max_results=5,
            timeout_seconds=timeout_seconds,
            before_provider_call=before_provider_call,
        )
        if retry_authorization is not None and retry_store is not None:
            retry_store.record_context(retry_authorization)
        if len(context.sources) < 3:
            raise NonRetryableAgentError(
                "RETRIEVAL_NO_RESULTS: General Opinion requires at least three independent "
                "retrieved sources before structured reasoning",
                automatic_retry_allowed=False,
            )
        provider_input = canonical.model_dump(mode="json")
        provider_input["retrieval_context"] = context.model_dump(mode="json")
        return provider_input

    def normalize_provider_output(
        self,
        raw: dict,
        *,
        provider_input: dict | None = None,
    ) -> dict:
        hydrated = deepcopy(raw)
        retrieved = {
            item["source_id"]: item
            for item in (provider_input or {}).get("retrieval_context", {}).get(
                "sources", []
            )
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        }
        general_opinion = hydrated.get("general_opinion")
        sources = (
            general_opinion.get("supporting_sources")
            if isinstance(general_opinion, dict)
            else None
        )
        if not isinstance(sources, list):
            return hydrated
        for source in sources:
            if not isinstance(source, dict):
                continue
            basis = retrieved.get(source.get("source_id"))
            if basis is None:
                continue
            source["source"] = basis.get("title")
            source["url"] = basis.get("url")
        return hydrated

    def validate_output_contract(
        self,
        input_payload: BaseModel,
        output_payload: BaseModel,
        *,
        provider_input: dict | None = None,
    ) -> BaseModel:
        result = GeneralOpinionOutput.model_validate(output_payload)
        retrieved_sources = {
            str(item.get("source_id")): item
            for item in (provider_input or {}).get("retrieval_context", {}).get("sources", [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        }
        unsupported = [
            str(item.source_id or item.url)
            for item in result.general_opinion.supporting_sources
            if (
                item.source_id not in retrieved_sources
                or str(item.url) != str(retrieved_sources[item.source_id].get("url"))
                or item.source != retrieved_sources[item.source_id].get("title")
            )
        ]
        if unsupported:
            raise NonRetryableAgentError(
                "OUTPUT_CONTRACT_ERROR: General Opinion contains source references absent "
                "from or changed relative to retrieval "
                f"context: {', '.join(unsupported)}",
                automatic_retry_allowed=False,
            )
        source_ids = [
            item.source_id for item in result.general_opinion.supporting_sources
        ]
        if len(source_ids) != len(set(source_ids)):
            raise NonRetryableAgentError(
                "OUTPUT_CONTRACT_ERROR: General Opinion supporting source IDs must be unique",
                automatic_retry_allowed=False,
            )
        return result
