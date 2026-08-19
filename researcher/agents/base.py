from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Callable
import unicodedata
from urllib.parse import urlsplit

from pydantic import BaseModel

from common.agents import StructuredAgent
from common.models.pmp import MessageType, PMPMessage
from common.models.errors import NonRetryableAgentError
from common.provider_runtime_model_repair import RUNTIME_MODEL_REPAIR_SUFFIX
from common.provider_runtime_output_repair import (
    RUNTIME_ADAPTER_REPAIR_SUFFIX,
    RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX,
    RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
    RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX,
)
from common.retrieval_reconstruction import RETRIEVAL_RECONSTRUCTION_SUFFIX
from researcher.schemas.source import (
    GROUNDED_IDENTITY_METADATA,
    PROVENANCE_HYDRATED_METADATA,
    ResearchSourceType,
)
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.research_task import ResearchTask
from retrieval import RetrievalStrategy


RELEVANT_EXCERPT_MAX_CHARS = 1_000
COUNTRY_ALIASES_BY_DOMAIN_SUFFIX = {
    ".jp": {"japan", "日本", "日本国"},
    ".eu": {"eu", "europeanunion", "欧州連合"},
    ".gov": {"unitedstates", "usa", "us", "米国", "アメリカ合衆国"},
    ".gov.uk": {"unitedkingdom", "uk", "英国", "イギリス"},
    ".de": {"germany", "deutschland", "ドイツ"},
    ".fr": {"france", "フランス"},
    ".ca": {"canada", "カナダ"},
    ".au": {"australia", "オーストラリア"},
}
COUNTRY_LABEL_BY_DOMAIN_SUFFIX = (
    (".gov.uk", "United Kingdom"),
    (".jp", "日本"),
    (".eu", "EU"),
    (".gov", "United States"),
    (".de", "Germany"),
    (".fr", "France"),
    (".ca", "Canada"),
    (".au", "Australia"),
)
GOVERNMENT_ORGANIZATION_ALIASES_BY_DOMAIN_SUFFIX = {
    "cas.go.jp": {"内閣官房", "cabinet secretariat"},
    "bunka.go.jp": {"文化庁", "agency for cultural affairs"},
    "copyright.gov": {
        "u.s. copyright office",
        "us copyright office",
        "united states copyright office",
    },
    "euipo.europa.eu": {
        "euipo",
        "european union intellectual property office",
    },
    "bundesnetzagentur.de": {
        "bundesnetzagentur",
        "federal network agency",
    },
}
CANONICAL_OFFICIAL_SOURCE_LABEL_BY_HOST = {
    "cas.go.jp": "内閣官房",
    "bunka.go.jp": "文化庁",
    "copyright.gov": "U.S. Copyright Office",
    "euipo.europa.eu": "European Union Intellectual Property Office",
    "bundesnetzagentur.de": "Bundesnetzagentur",
}
IDENTITY_COMPONENT_SEPARATOR = re.compile(r"\s*(?:/|／|\||｜)\s*")


def canonical_grounding_text(value: object) -> str:
    """Compare identity text independent of layout whitespace and punctuation."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def country_is_grounded(country: str, *, url: object, grounding: str) -> bool:
    canonical_country = canonical_grounding_text(country)
    if canonical_country and canonical_country in grounding:
        return True
    hostname = (urlsplit(str(url or "")).hostname or "").casefold()
    return any(
        hostname.endswith(suffix)
        and canonical_country
        in {canonical_grounding_text(alias) for alias in aliases}
        for suffix, aliases in COUNTRY_ALIASES_BY_DOMAIN_SUFFIX.items()
    )


def canonical_country_from_url(url: object) -> str | None:
    """Return only a country/region deterministically encoded by the hostname."""

    hostname = (urlsplit(str(url or "")).hostname or "").casefold()
    return next(
        (
            label
            for suffix, label in COUNTRY_LABEL_BY_DOMAIN_SUFFIX
            if hostname.endswith(suffix)
        ),
        None,
    )


def canonical_source_label(
    url: object,
    title: object,
    *,
    source_type: ResearchSourceType | None,
) -> str:
    """Build a stable source label without asking the reasoning Provider."""

    hostname = (urlsplit(str(url or "")).hostname or "").casefold().rstrip(".")
    if source_type == ResearchSourceType.GOVERNMENT:
        for suffix, label in CANONICAL_OFFICIAL_SOURCE_LABEL_BY_HOST.items():
            if hostname == suffix or hostname.endswith("." + suffix):
                return label
    if hostname:
        return hostname.removeprefix("www.")
    title_text = str(title or "").strip()
    return title_text or "unverified-source"


def hydrated_provenance_metadata(
    source_type: ResearchSourceType,
    *,
    url: object,
    title: object,
) -> dict[str, str | None]:
    """Hydrate non-generative provenance fields from immutable Retrieval data."""

    label = canonical_source_label(url, title, source_type=source_type)
    values: dict[ResearchSourceType, dict[str, str | None]] = {
        ResearchSourceType.EXPERT: {
            "expert_name": None,
            "affiliation": None,
        },
        ResearchSourceType.ACADEMIC: {"journal_name": label},
        ResearchSourceType.GOVERNMENT: {
            "organization": label,
            "country": canonical_country_from_url(url),
        },
        ResearchSourceType.NEWS: {"media_name": label},
        ResearchSourceType.PUBLIC_OPINION: {"platform": label},
        ResearchSourceType.POLITICIAN: {
            "politician_name": None,
            "party": None,
            "position": None,
        },
        ResearchSourceType.INDUSTRY: {"organization_name": label},
    }
    hydrated = values[source_type]
    if set(hydrated) != PROVENANCE_HYDRATED_METADATA[source_type]:
        raise RuntimeError(f"Incomplete provenance hydration for {source_type.value}")
    return hydrated


def identity_value_is_grounded(
    value: str,
    *,
    source_type: ResearchSourceType,
    url: object,
    grounding: str,
) -> bool:
    """Ground an identity as a whole or as bounded government components."""

    canonical = canonical_grounding_text(value)
    if canonical and canonical in grounding:
        return True
    if source_type != ResearchSourceType.GOVERNMENT:
        return False
    hostname = (urlsplit(str(url or "")).hostname or "").casefold()
    aliases = {
        canonical_grounding_text(alias)
        for suffix, values in GOVERNMENT_ORGANIZATION_ALIASES_BY_DOMAIN_SUFFIX.items()
        if hostname.endswith(suffix)
        for alias in values
    }
    if canonical in aliases:
        return True
    parts = [part for part in IDENTITY_COMPONENT_SEPARATOR.split(value) if part.strip()]
    if len(parts) < 2:
        return False
    return all(
        (part_canonical := canonical_grounding_text(part))
        and (part_canonical in grounding or part_canonical in aliases)
        for part in parts
    )


class ResearcherAgent(StructuredAgent):
    """Researcher adapter, including the research-revision message mapping."""

    input_schema: type[BaseModel] = ResearchTask
    output_schema: type[BaseModel] = ResearchResult
    prompt_layer = "researcher"
    manager_agent_id = "researcher.manager"
    result_objective_suffix = "evidence collection result"
    accepted_message_types = {
        MessageType.TASK.value,
        MessageType.INFO.value,
        MessageType.RESEARCH_REVISION_REQUEST.value,
    }

    async def prepare_provider_input(
        self,
        payload: BaseModel,
        *,
        message: PMPMessage,
        timeout_seconds: int,
    ) -> dict:
        if self.input_schema is not ResearchTask:
            return await super().prepare_provider_input(
                payload,
                message=message,
                timeout_seconds=timeout_seconds,
            )
        task = ResearchTask.model_validate(payload)
        if self.retrieval_coordinator is None:
            raise NonRetryableAgentError(
                f"RETRIEVAL_NOT_CONFIGURED: {self.agent_id} requires a retrieval provider",
                automatic_retry_allowed=False,
            )
        retrieval_task_id = message.metadata.extensions.get(
            "retrieval_task_id",
            task.task_id,
        )
        if retrieval_task_id != task.task_id:
            provider_task_id = message.metadata.extensions.get("provider_task_id")
            if (
                not isinstance(retrieval_task_id, str)
                or not retrieval_task_id.endswith(RETRIEVAL_RECONSTRUCTION_SUFFIX)
                or not isinstance(provider_task_id, str)
                or not provider_task_id.endswith(
                    (
                        RUNTIME_MODEL_REPAIR_SUFFIX,
                        RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
                        RUNTIME_ADAPTER_REPAIR_SUFFIX,
                        RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX,
                        RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX,
                    )
                )
            ):
                raise NonRetryableAgentError(
                    "RETRIEVAL_CONTEXT_OVERRIDE_REJECTED: reconstructed Retrieval may "
                    "only be used by an authorized runtime model repair",
                    automatic_retry_allowed=False,
                )
        context = await self.prepare_retrieval_context(
            task,
            workflow_id=message.workflow_id,
            retrieval_task_id=str(retrieval_task_id),
            timeout_seconds=timeout_seconds,
        )
        provider_input = task.model_dump(mode="json")
        provider_input["retrieval_context"] = context.model_dump(mode="json")
        return provider_input

    async def prepare_retrieval_context(
        self,
        task: ResearchTask,
        *,
        workflow_id: str,
        retrieval_task_id: str,
        timeout_seconds: int,
        before_provider_call: Callable[[Path, str], None] | None = None,
    ):
        """Prepare one persisted context without invoking Structured Reasoning."""

        if self.retrieval_coordinator is None:
            raise NonRetryableAgentError(
                f"RETRIEVAL_NOT_CONFIGURED: {self.agent_id} requires a retrieval provider",
                automatic_retry_allowed=False,
            )
        strategy, query = self.retrieval_request(task)
        return await self.retrieval_coordinator.prepare(
            workflow_id=workflow_id,
            task_id=retrieval_task_id,
            research_question_id=task.research_question_id,
            agent_id=self.agent_id,
            strategy=strategy,
            queries=[query],
            max_results=task.max_sources,
            timeout_seconds=timeout_seconds,
            before_provider_call=before_provider_call,
        )

    @staticmethod
    def retrieval_request(task: ResearchTask) -> tuple[RetrievalStrategy, str]:
        """Return the canonical Retrieval strategy and deterministic query."""

        strategy = RetrievalStrategy(task.research_target)
        strategy_terms = {
            RetrievalStrategy.EXPERT: "専門家 研究者 インタビュー 発言",
            RetrievalStrategy.ACADEMIC: "査読 論文 研究 DOI",
            RetrievalStrategy.GOVERNMENT: "政府 省庁 統計 公式資料",
            RetrievalStrategy.NEWS: "報道 ニュース 記事",
            RetrievalStrategy.PUBLIC_OPINION: "世論調査 公開アンケート 市民意見",
            RetrievalStrategy.POLITICIAN: "国会 政党 政治家 公式発言",
            RetrievalStrategy.INDUSTRY: "企業 業界団体 調査 報告",
        }[strategy]
        query = " ".join(
            [task.question, *task.scope, strategy_terms, *task.constraints]
        )
        if task.revision_context is not None:
            query += " revision_requirements=" + json.dumps(
                task.revision_context,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        return strategy, query

    def normalize_provider_output(
        self,
        raw: dict,
        *,
        provider_input: dict | None = None,
    ) -> dict:
        if self.output_schema is not ResearchResult:
            return super().normalize_provider_output(
                raw,
                provider_input=provider_input,
            )
        hydrated = deepcopy(raw)
        retrieved = {
            item["source_id"]: item
            for item in (provider_input or {}).get("retrieval_context", {}).get(
                "sources", []
            )
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        }
        sources = hydrated.get("sources")
        if not isinstance(sources, list):
            return hydrated
        for source in sources:
            if not isinstance(source, dict):
                continue
            basis = retrieved.get(source.get("source_id"))
            if basis is None:
                continue
            source["title"] = basis.get("title")
            source["url"] = basis.get("url")
            source["retrieved_at"] = basis.get("retrieved_at")
            try:
                source_type = ResearchSourceType(source.get("source_type"))
            except (TypeError, ValueError):
                source_type = None
            metadata = source.get("source_specific_metadata")
            label = canonical_source_label(
                basis.get("url"),
                basis.get("title"),
                source_type=source_type,
            )
            source["source_name"] = label
            source["author_or_organization"] = label
            source["published_at"] = None
            if source_type is not None and isinstance(metadata, dict):
                metadata.update(
                    hydrated_provenance_metadata(
                        source_type,
                        url=basis.get("url"),
                        title=basis.get("title"),
                    )
                )
            content = basis.get("content")
            if isinstance(content, str) and content.strip():
                # An excerpt is a quotation, not a reasoning field.  Restore a
                # bounded exact slice from the immutable Retrieval checkpoint so
                # provider whitespace/paraphrase cannot break traceability.
                source["relevant_excerpt"] = content.strip()[
                    :RELEVANT_EXCERPT_MAX_CHARS
                ]
            else:
                source["relevant_excerpt"] = None
        return hydrated

    def validate_output_contract(
        self,
        input_payload: BaseModel,
        output_payload: BaseModel,
        *,
        provider_input: dict | None = None,
    ) -> BaseModel:
        if self.output_schema is not ResearchResult:
            return super().validate_output_contract(
                input_payload,
                output_payload,
                provider_input=provider_input,
            )
        result = ResearchResult.model_validate(output_payload)
        context = (provider_input or {}).get("retrieval_context")
        if not isinstance(context, dict):
            raise NonRetryableAgentError(
                "OUTPUT_CONTRACT_ERROR: ResearchResult has no persisted retrieval context",
                automatic_retry_allowed=False,
            )
        retrieved = {
            item["source_id"]: item
            for item in context.get("sources", [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        }
        for source in result.sources:
            basis = retrieved.get(source.source_id)
            if basis is None:
                raise NonRetryableAgentError(
                    "OUTPUT_CONTRACT_ERROR: ResearchResult contains a source_id absent "
                    f"from retrieval context: {source.source_id}",
                    automatic_retry_allowed=False,
                )
            if str(source.url) != str(basis.get("url")):
                raise NonRetryableAgentError(
                    f"OUTPUT_CONTRACT_ERROR: source URL changed for {source.source_id}",
                    automatic_retry_allowed=False,
                )
            if source.title != basis.get("title"):
                raise NonRetryableAgentError(
                    f"OUTPUT_CONTRACT_ERROR: source title changed for {source.source_id}",
                    automatic_retry_allowed=False,
                )
            grounding = canonical_grounding_text(" ".join(
                str(basis.get(field) or "") for field in ("title", "url", "content")
            ))
            if (
                source.relevant_excerpt
                and canonical_grounding_text(source.relevant_excerpt) not in grounding
            ):
                raise NonRetryableAgentError(
                    f"OUTPUT_CONTRACT_ERROR: excerpt is absent from retrieval context for "
                    f"{source.source_id}",
                    automatic_retry_allowed=False,
                )
            identity_values = [source.source_name, source.author_or_organization]
            source_type = ResearchSourceType(source.source_type)
            identity_values.extend(
                source.source_specific_metadata.get(field)
                for field in GROUNDED_IDENTITY_METADATA[
                    source_type
                ]
            )
            unsupported = sorted(
                {
                    value
                    for value in identity_values
                    if isinstance(value, str)
                    and value.strip()
                    and not identity_value_is_grounded(
                        value,
                        source_type=source_type,
                        url=basis.get("url"),
                        grounding=grounding,
                    )
                }
            )
            if source.source_type == ResearchSourceType.GOVERNMENT.value:
                country = source.source_specific_metadata.get("country")
                if (
                    isinstance(country, str)
                    and country.strip()
                    and not country_is_grounded(
                        country,
                        url=basis.get("url"),
                        grounding=grounding,
                    )
                ):
                    unsupported.append(country)
            if unsupported:
                raise NonRetryableAgentError(
                    "OUTPUT_CONTRACT_ERROR: ungrounded source identity metadata for "
                    f"{source.source_id}: {', '.join(unsupported)}",
                    automatic_retry_allowed=False,
                )
        return result

    def resolve_result_message_type(self, request: PMPMessage) -> MessageType:
        if request.message_type == MessageType.RESEARCH_REVISION_REQUEST.value:
            return MessageType.RESEARCH_REVISION_RESULT
        return self.output_message_type
