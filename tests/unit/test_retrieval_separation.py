from __future__ import annotations

import asyncio
from copy import deepcopy
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from common.agents.base import StructuredAgent
from common.models.errors import NonRetryableAgentError
from common.models.pmp import MessageType, PMPMessage
from common.provider_retry import OPERATOR_RETRY_SUFFIX
from common.structured_outputs import strict_output_schema
from producer.schemas.general_opinion import GeneralOpinionOutput
from producer.registry import ProducerRegistry
from producer.schemas.general_opinion import GeneralOpinionInput
from producer.schemas.topic_selector import SelectedTopic
from providers.mock_provider import MockModelProvider
from researcher.registry import ResearcherRegistry
from researcher.agents.base import (
    RELEVANT_EXCERPT_MAX_CHARS,
    hydrated_provenance_metadata,
)
from researcher.schemas.research_result import ResearchResult
from researcher.schemas.research_task import RESEARCH_TARGET_MAP, ResearchTask
from researcher.schemas.source import (
    GROUNDED_IDENTITY_METADATA,
    PROVENANCE_HYDRATED_METADATA,
    SOURCE_METADATA_MODELS,
    ResearchSourceType,
)
from retrieval import (
    MockRetrievalProvider,
    OpenRouterWebSearchProvider,
    RetrievalCoordinator,
    RetrievalStrategy,
)


def research_task(task_id: str = "task_retrieval") -> ResearchTask:
    return ResearchTask(
        task_id=task_id,
        research_question_id="rq_retrieval",
        target_agent_id="researcher.academic_researcher",
        research_target="ACADEMIC",
        question="生成AIは雇用へどのような影響を与えるか",
        scope=["日本", "2022年以降"],
        constraints=["一次資料を優先する"],
        max_sources=3,
        revision_context=None,
    )


def task_message(
    task: ResearchTask,
    workflow_id: str = "00000000-0000-4000-8000-000000000029",
) -> PMPMessage:
    return PMPMessage.create(
        workflow_id=workflow_id,
        sender_agent_id="researcher.manager",
        receiver_agent_id=task.target_agent_id,
        message_type=MessageType.TASK,
        objective="retrieve then reason",
        payload=task.model_dump(mode="json"),
    )


class RecordingMockProvider(MockModelProvider):
    def __init__(self, *, data_dir: Path) -> None:
        super().__init__(reservation_root=data_dir / "provider_call_reservations")
        self.preflight_input: dict | None = None
        self.generated_input: dict | None = None
        self.data_dir = data_dir

    def validate_request_budget(self, **kwargs) -> int:
        self.preflight_input = kwargs["input_data"]
        return 1

    async def generate_structured(self, **kwargs):
        self.generated_input = kwargs["input_data"]
        retrieval_id = self.generated_input["retrieval_context"]["retrieval_id"]
        workflow_id = self.generated_input["retrieval_context"]["workflow_id"]
        context_path = (
            self.data_dir
            / "retrieval_contexts"
            / workflow_id
            / f"{retrieval_id}.json"
        )
        if not context_path.exists():
            raise AssertionError("retrieval context was not persisted before LLM invocation")
        raw = await super().generate_structured(**kwargs)
        # Mirror the actual specialized provider schema: immutable Retrieval
        # metadata is intentionally absent and must be hydrated by the agent.
        for source in raw.get("sources", []):
            source.pop("title", None)
            source.pop("url", None)
            source.pop("retrieved_at", None)
        return raw


class RetrievalSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_openrouter_retrieval_forces_server_search_and_parses_citations(self) -> None:
        provider = OpenRouterWebSearchProvider(
            api_key="test-key",
            model="google/gemini-3.7-flash",
        )
        envelope = {
            "choices": [
                {
                    "message": {
                        "annotations": [
                            {
                                "type": "url_citation",
                                "url_citation": {
                                    "url": "https://example.com/source",
                                    "title": "Grounded source",
                                    "content": "Grounded excerpt",
                                },
                            }
                        ]
                    }
                }
            ]
        }
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(envelope).encode("utf-8")
        captured_request = None

        def fake_urlopen(request, timeout):
            nonlocal captured_request
            captured_request = request
            self.assertEqual(timeout, 10)
            return response

        with patch("retrieval.providers.urlopen", side_effect=fake_urlopen):
            results = await provider.search(
                query="grounded query",
                strategy=RetrievalStrategy.NEWS,
                max_results=3,
                timeout_seconds=10,
            )

        body = json.loads(captured_request.data.decode("utf-8"))
        self.assertEqual(body["tool_choice"], "required")
        self.assertEqual(body["tools"][0]["type"], "openrouter:web_search")
        self.assertEqual(body["tools"][0]["parameters"]["max_total_results"], 3)
        self.assertEqual(str(results[0].url), "https://example.com/source")
        self.assertEqual(results[0].content, "Grounded excerpt")

    async def test_openrouter_batch_retrieval_uses_saved_reservation_and_server_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reservation = (
                Path(temporary)
                / "retrieval_call_reservations"
                / "openrouter_web_search"
                / "workflow"
                / "retrieval.json"
            )
            reservation.parent.mkdir(parents=True)
            reservation.write_text("{}", encoding="utf-8")
            provider = OpenRouterWebSearchProvider(
                api_key="test-key",
                model="google/gemini-3.7-flash:batch",
                batch_poll_interval_seconds=0,
            )
            submitted = None

            def response(payload):
                value = MagicMock()
                value.__enter__.return_value = value
                value.read.return_value = json.dumps(payload).encode("utf-8")
                return value

            def fake_urlopen(request, timeout):
                nonlocal submitted
                if request.method == "POST":
                    submitted = json.loads(request.data.decode("utf-8"))
                    return response({"id": "batch_retrieval", "status": "validating"})
                custom_id = submitted["requests"][0]["custom_id"]
                return response(
                    {
                        "id": "batch_retrieval",
                        "status": "completed",
                        "results": [
                            {
                                "custom_id": custom_id,
                                "response": {
                                    "status_code": 200,
                                    "body": {
                                        "id": "gen-retrieval",
                                        "choices": [
                                            {
                                                "message": {
                                                    "annotations": [
                                                        {
                                                            "type": "url_citation",
                                                            "url_citation": {
                                                                "url": "https://example.com/batch",
                                                                "title": "Batch source",
                                                                "content": "Batch excerpt",
                                                            },
                                                        }
                                                    ]
                                                }
                                            }
                                        ],
                                    },
                                },
                                "error": None,
                            }
                        ],
                    }
                )

            with patch("providers.openrouter_batch.urlopen", side_effect=fake_urlopen):
                results = await provider.search(
                    query="batch query",
                    strategy=RetrievalStrategy.NEWS,
                    max_results=3,
                    timeout_seconds=10,
                    invocation_reservation_path=reservation,
                    invocation_discriminator="retrieval-test",
                )

            self.assertEqual(submitted["model"], "google/gemini-3.7-flash")
            request_body = submitted["requests"][0]["body"]
            self.assertEqual(request_body["model"], "google/gemini-3.7-flash")
            self.assertNotIn("provider", request_body)
            self.assertEqual(request_body["tool_choice"], "required")
            self.assertEqual(request_body["tools"][0]["type"], "openrouter:web_search")
            self.assertEqual(str(results[0].url), "https://example.com/batch")

    async def test_default_prepare_provider_input_is_exact_noop(self) -> None:
        payload = research_task()
        agent = object.__new__(StructuredAgent)
        prepared = await agent.prepare_provider_input(
            payload,
            message=task_message(payload),
            timeout_seconds=10,
        )
        self.assertEqual(prepared, payload.model_dump(mode="json"))

    async def test_research_planner_does_not_receive_retrieval_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            registry = ProducerRegistry(provider)
            planner = registry.get("producer.research_planner")
            payload = planner.input_schema.model_validate(
                {
                    "selected_topic": {
                        "topic_id": "topic_1",
                        "title": "test",
                        "selection_reason": "test selection",
                    },
                    "general_opinion": {
                        "general_opinion_id": "opinion_1",
                        "statement": "test statement",
                        "confidence": 0.5,
                        "evidence_summary": "test evidence",
                        "supporting_sources": [
                            {"source": f"source {index}", "url": f"https://example.invalid/{index}"}
                            for index in range(3)
                        ],
                    },
                    "revision_context": None,
                }
            )
            message = PMPMessage.create(
                workflow_id="00000000-0000-4000-8000-000000000030",
                sender_agent_id="producer.manager",
                receiver_agent_id=planner.agent_id,
                message_type=MessageType.TASK,
                objective="plan only",
                payload=payload.model_dump(mode="json"),
            )
            prepared = await planner.prepare_provider_input(
                payload,
                message=message,
                timeout_seconds=10,
            )
            self.assertEqual(prepared, payload.model_dump(mode="json"))
            self.assertNotIn("retrieval_context", prepared)

    async def test_researcher_adds_persisted_context_without_mutating_task(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            provider = RecordingMockProvider(data_dir=data_dir)
            agent = ResearcherRegistry(
                provider,
                retrieval_coordinator=coordinator,
            ).get("researcher.academic_researcher")
            payload = research_task()
            canonical_before = payload.model_dump(mode="json")
            response = await agent.execute(task_message(payload))

            self.assertEqual(response.message_type, MessageType.RESULT.value)
            self.assertEqual(payload.model_dump(mode="json"), canonical_before)
            self.assertIsNotNone(provider.generated_input)
            self.assertIn("retrieval_context", provider.generated_input)
            self.assertEqual(provider.preflight_input, provider.generated_input)
            self.assertEqual(retrieval_provider.calls, 1)
            retrieved = provider.generated_input["retrieval_context"]["sources"][0]
            returned = response.payload["sources"][0]
            self.assertEqual(returned["title"], retrieved["title"])
            self.assertEqual(returned["url"], retrieved["url"])
            self.assertEqual(returned["retrieved_at"], retrieved["retrieved_at"])
            self.assertEqual(
                returned["relevant_excerpt"],
                retrieved["content"].strip()[:RELEVANT_EXCERPT_MAX_CHARS],
            )

    async def test_researcher_overwrites_paraphrased_excerpt_with_exact_saved_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            agent = ResearcherRegistry(
                provider,
                retrieval_coordinator=coordinator,
            ).get("researcher.academic_researcher")
            payload = research_task()
            prepared = await agent.prepare_provider_input(
                payload,
                message=task_message(payload),
                timeout_seconds=10,
            )
            original_content = prepared["retrieval_context"]["sources"][0][
                "content"
            ]
            prepared["retrieval_context"]["sources"][0]["content"] = (
                original_content + " exact persisted quotation " + "x" * 2_000
            )
            raw = await provider.generate_structured(
                model="mock",
                system_prompt="test",
                input_data=prepared,
                output_schema=ResearchResult,
                timeout_seconds=10,
            )
            raw["sources"][0]["relevant_excerpt"] = "model paraphrase"
            normalized = agent.normalize_provider_output(
                raw,
                provider_input=prepared,
            )
            expected = prepared["retrieval_context"]["sources"][0][
                "content"
            ].strip()[:RELEVANT_EXCERPT_MAX_CHARS]
            self.assertEqual(normalized["sources"][0]["relevant_excerpt"], expected)
            self.assertEqual(len(expected), RELEVANT_EXCERPT_MAX_CHARS)
            validated = agent.validate_output_contract(
                payload,
                ResearchResult.model_validate(normalized),
                provider_input=prepared,
            )
            self.assertEqual(validated.sources[0].relevant_excerpt, expected)

    async def test_researcher_identity_grounding_normalizes_layout_and_country_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            payload = research_task().model_copy(
                update={
                    "target_agent_id": "researcher.government_researcher",
                    "research_target": "GOVERNMENT",
                }
            )
            agent = ResearcherRegistry(
                provider,
                retrieval_coordinator=coordinator,
            ).get(payload.target_agent_id)
            prepared = await agent.prepare_provider_input(
                payload,
                message=task_message(payload),
                timeout_seconds=10,
            )
            basis = prepared["retrieval_context"]["sources"][0]
            basis["url"] = "https://www.cas.go.jp/document.pdf"
            basis["content"] += " 内閣府知的財産戦略推 進事務局 "
            raw = await provider.generate_structured(
                model="mock",
                system_prompt="test",
                input_data=prepared,
                output_schema=ResearchResult,
                timeout_seconds=10,
            )
            source = raw["sources"][0]
            source["source_name"] = "Provider-generated publisher spelling"
            source["author_or_organization"] = "Provider-generated organization spelling"
            source["source_specific_metadata"] = {
                "organization": "内閣官房 / 内閣府知的財産戦略推進事務局",
                "country": "Japan",
                "document_type": "POLICY_DRAFT",
            }
            normalized = agent.normalize_provider_output(raw, provider_input=prepared)
            self.assertEqual(
                normalized["sources"][0]["source_name"],
                "内閣官房",
            )
            self.assertEqual(
                normalized["sources"][0]["author_or_organization"],
                normalized["sources"][0]["source_name"],
            )
            validated = agent.validate_output_contract(
                payload,
                ResearchResult.model_validate(normalized),
                provider_input=prepared,
            )
            self.assertEqual(
                validated.sources[0].source_specific_metadata["organization"],
                "内閣官房",
            )
            self.assertEqual(
                validated.sources[0].source_specific_metadata["country"],
                "日本",
            )

            unrelated = deepcopy(normalized)
            unrelated["sources"][0]["source_specific_metadata"]["organization"] = (
                "総務省 / 内閣府知的財産戦略推進事務局"
            )
            with self.assertRaisesRegex(
                NonRetryableAgentError,
                "ungrounded source identity metadata",
            ):
                agent.validate_output_contract(
                    payload,
                    ResearchResult.model_validate(unrelated),
                    provider_input=prepared,
                )

            normalized["sources"][0]["source_specific_metadata"]["country"] = (
                "France"
            )
            with self.assertRaisesRegex(
                NonRetryableAgentError,
                "ungrounded source identity metadata",
            ):
                agent.validate_output_contract(
                    payload,
                    ResearchResult.model_validate(normalized),
                    provider_input=prepared,
                )

    def test_grounded_identity_fields_exclude_analytical_classifications(self) -> None:
        self.assertNotIn(
            "study_type",
            GROUNDED_IDENTITY_METADATA[ResearchSourceType.ACADEMIC],
        )
        self.assertNotIn(
            "article_type",
            GROUNDED_IDENTITY_METADATA[ResearchSourceType.NEWS],
        )
        self.assertNotIn(
            "statement_type",
            GROUNDED_IDENTITY_METADATA[ResearchSourceType.POLITICIAN],
        )
        self.assertNotIn(
            "organization_type",
            GROUNDED_IDENTITY_METADATA[ResearchSourceType.INDUSTRY],
        )

    def test_all_researcher_provenance_fields_are_deterministically_hydrated(self) -> None:
        urls = {
            ResearchSourceType.EXPERT: "https://experts.example/interview",
            ResearchSourceType.ACADEMIC: "https://journals.example/article",
            ResearchSourceType.GOVERNMENT: "https://www.cas.go.jp/document.pdf",
            ResearchSourceType.NEWS: "https://news.example/report",
            ResearchSourceType.PUBLIC_OPINION: "https://forum.example/thread",
            ResearchSourceType.POLITICIAN: "https://parliament.example/record",
            ResearchSourceType.INDUSTRY: "https://association.example/report",
        }
        for source_type, url in urls.items():
            with self.subTest(source_type=source_type.value):
                hydrated = hydrated_provenance_metadata(
                    source_type,
                    url=url,
                    title="Provider-independent title",
                )
                self.assertEqual(
                    set(hydrated),
                    PROVENANCE_HYDRATED_METADATA[source_type],
                )
                self.assertNotIn(
                    "Provider-generated identity",
                    hydrated.values(),
                )
        self.assertEqual(
            hydrated_provenance_metadata(
                ResearchSourceType.GOVERNMENT,
                url=urls[ResearchSourceType.GOVERNMENT],
                title="ignored",
            ),
            {"organization": "内閣官房", "country": "日本"},
        )
        self.assertEqual(
            hydrated_provenance_metadata(
                ResearchSourceType.EXPERT,
                url=urls[ResearchSourceType.EXPERT],
                title="Expert interview",
            )["expert_name"],
            None,
        )
        self.assertEqual(
            hydrated_provenance_metadata(
                ResearchSourceType.ACADEMIC,
                url="https://copyright.gov/report.pdf",
                title="A cross-category source",
            )["journal_name"],
            "copyright.gov",
        )
        self.assertEqual(
            hydrated_provenance_metadata(
                ResearchSourceType.POLITICIAN,
                url=urls[ResearchSourceType.POLITICIAN],
                title="Parliament record",
            )["politician_name"],
            None,
        )

    def test_all_researcher_strict_schemas_remove_redundant_identity_aliases(self) -> None:
        for target, agent_id in RESEARCH_TARGET_MAP.items():
            with self.subTest(target=target.value):
                schema = strict_output_schema(
                    ResearchResult,
                    input_data={
                        "research_target": target.value,
                        "target_agent_id": agent_id,
                        "retrieval_context": {
                            "sources": [{"source_id": "source_allowed"}]
                        },
                    },
                )
                properties = schema["$defs"]["ResearchSource"]["properties"]
                self.assertNotIn("source_name", properties)
                self.assertNotIn("author_or_organization", properties)
                self.assertNotIn("published_at", properties)
                source_type = ResearchSourceType(target.value)
                metadata = schema["$defs"][
                    SOURCE_METADATA_MODELS[source_type].__name__
                ]["properties"]
                for field_name in PROVENANCE_HYDRATED_METADATA[source_type]:
                    self.assertNotIn(field_name, metadata)
        target, agent_id = next(iter(RESEARCH_TARGET_MAP.items()))
        empty_schema = strict_output_schema(
            ResearchResult,
            input_data={
                "research_target": target.value,
                "target_agent_id": agent_id,
                "retrieval_context": {"sources": []},
            },
        )
        empty_properties = empty_schema["$defs"]["ResearchSource"]["properties"]
        self.assertNotIn("source_name", empty_properties)
        self.assertNotIn("author_or_organization", empty_properties)
        self.assertNotIn("published_at", empty_properties)
        self.assertEqual(empty_schema["properties"]["sources"]["maxItems"], 0)

    async def test_llm_retry_identity_reuses_retrieval_without_search_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            agent = ResearcherRegistry(
                provider,
                retrieval_coordinator=coordinator,
            ).get("researcher.academic_researcher")
            original = research_task()
            first = await agent.prepare_provider_input(
                original,
                message=task_message(original),
                timeout_seconds=10,
            )
            retry = research_task(original.task_id + OPERATOR_RETRY_SUFFIX)
            second = await agent.prepare_provider_input(
                retry,
                message=task_message(retry),
                timeout_seconds=10,
            )

            self.assertEqual(retrieval_provider.calls, 1)
            self.assertEqual(
                first["retrieval_context"]["retrieval_id"],
                second["retrieval_context"]["retrieval_id"],
            )

    async def test_retrieval_failure_reservation_blocks_automatic_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            provider.failure = NonRetryableAgentError("RETRIEVAL_PROVIDER_ERROR")
            coordinator = RetrievalCoordinator(
                provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            arguments = {
                "workflow_id": "workflow_fault",
                "task_id": "task_fault",
                "research_question_id": "rq_fault",
                "agent_id": "researcher.academic_researcher",
                "strategy": RetrievalStrategy.ACADEMIC,
                "queries": ["fault query"],
                "max_results": 3,
                "timeout_seconds": 10,
            }
            with self.assertRaisesRegex(NonRetryableAgentError, "RETRIEVAL_PROVIDER_ERROR"):
                await coordinator.prepare(**arguments)
            with self.assertRaisesRegex(NonRetryableAgentError, "RETRIEVAL_AMBIGUOUS_STATE"):
                await coordinator.prepare(**arguments)
            self.assertEqual(provider.calls, 1)

    async def test_research_result_rejects_source_absent_from_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            agent = ResearcherRegistry(
                provider,
                retrieval_coordinator=coordinator,
            ).get("researcher.academic_researcher")
            payload = research_task()
            prepared = await agent.prepare_provider_input(
                payload,
                message=task_message(payload),
                timeout_seconds=10,
            )
            raw = await provider.generate_structured(
                model="mock",
                system_prompt="test",
                input_data=prepared,
                output_schema=ResearchResult,
                timeout_seconds=10,
            )
            raw["sources"][0]["source_id"] = "source_hallucinated"
            hallucinated = ResearchResult.model_validate(raw)
            with self.assertRaisesRegex(
                NonRetryableAgentError,
                "source_id absent from retrieval context",
            ):
                agent.validate_output_contract(
                    payload,
                    hallucinated,
                    provider_input=prepared,
                )

    async def test_strict_schema_binds_research_and_opinion_sources_to_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            agent = ResearcherRegistry(
                provider,
                retrieval_coordinator=coordinator,
            ).get("researcher.academic_researcher")
            payload = research_task()
            prepared = await agent.prepare_provider_input(
                payload,
                message=task_message(payload),
                timeout_seconds=10,
            )
            schema = strict_output_schema(ResearchResult, input_data=prepared)
            retrieved = prepared["retrieval_context"]["sources"][0]
            source_schema = schema["$defs"]["ResearchSource"]
            self.assertEqual(
                source_schema["properties"]["source_id"]["enum"],
                [retrieved["source_id"]],
            )
            self.assertEqual(
                source_schema["properties"]["source_id"]["enum"],
                [retrieved["source_id"]],
            )
            self.assertNotIn("url", source_schema["properties"])
            self.assertNotIn("title", source_schema["properties"])
            self.assertNotIn("source_name", source_schema["properties"])
            self.assertNotIn("author_or_organization", source_schema["properties"])
            self.assertNotIn("retrieved_at", source_schema["properties"])
            self.assertNotIn("relevant_excerpt", source_schema["properties"])

            opinion_input = {
                "retrieval_context": {
                    "sources": [
                        {
                            "source_id": f"source_{index}",
                            "title": f"title {index}",
                            "url": f"https://example.com/{index}",
                        }
                        for index in range(3)
                    ]
                }
            }
            opinion_schema = strict_output_schema(
                GeneralOpinionOutput,
                input_data=opinion_input,
            )
            self.assertEqual(
                opinion_schema["$defs"]["SupportingSource"]["properties"]["source_id"]["enum"],
                [f"source_{index}" for index in range(3)],
            )
            self.assertEqual(
                set(opinion_schema["$defs"]["SupportingSource"]["properties"]),
                {"source_id"},
            )

    async def test_general_opinion_receives_lightweight_retrieval_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            provider = MockModelProvider(
                reservation_root=data_dir / "provider_call_reservations"
            )
            registry = ProducerRegistry(provider)
            registry.bind_retrieval_data_dir(data_dir)
            agent = registry.get("producer.general_opinion_analyst")
            payload = GeneralOpinionInput(
                selected_topic=SelectedTopic(
                    topic_id="topic_general",
                    title="生成AIと雇用",
                    selection_reason="test selection",
                ),
                revision_context=None,
            )
            message = PMPMessage.create(
                workflow_id="00000000-0000-4000-8000-000000000031",
                sender_agent_id="producer.manager",
                receiver_agent_id=agent.agent_id,
                message_type=MessageType.TASK,
                objective="general opinion",
                payload=payload.model_dump(mode="json"),
            )
            prepared = await agent.prepare_provider_input(
                payload,
                message=message,
                timeout_seconds=10,
            )
            self.assertEqual(
                prepared["retrieval_context"]["retrieval_strategy"],
                "GENERAL_OPINION",
            )
            self.assertEqual(len(prepared["retrieval_context"]["sources"]), 3)


if __name__ == "__main__":
    unittest.main()
