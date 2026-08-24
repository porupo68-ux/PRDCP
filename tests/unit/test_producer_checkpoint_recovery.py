from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from common.models.pmp import MessageStatus, MessageType, PMPContext, PMPMessage, PMPMetadata
from common.models.errors import NonRetryableAgentError
from common.provider_output_repair import ProviderOutputRepairStatus
from common.provider_retry import ProviderRetryStatus
from common.retrieval_provider_retry import RetrievalProviderRetryStatus
from common.role_definitions import RoleDefinitionLoader
from config.settings import BASE_DIR
from producer.manager import ProducerManager
from producer.registry import ProducerRegistry
from producer.state import ProducerWorkflowState
from retrieval import MockRetrievalProvider, RetrievalCoordinator, RetrievalStrategy
from retrieval.models import RetrievedContext, RetrievedSource
from storage.workflow_repository import WorkflowRepository


class OneCallGeneralOpinionProvider:
    provider_id = "openrouter"

    def __init__(self, data_dir: Path) -> None:
        self.reservation_root = data_dir / "provider_call_reservations"
        self.calls: list[str] = []
        self.fail = False
        self.allow_failed_batch_retry = False

    def can_retry_failed_invocation(self, **kwargs) -> bool:
        del kwargs
        return self.allow_failed_batch_retry

    def validate_request_budget(self, **kwargs) -> int:
        del kwargs
        return 1

    async def generate_structured(self, **kwargs) -> dict:
        self.calls.append(kwargs["output_schema"].__name__)
        if self.fail:
            raise NonRetryableAgentError(
                "injected provider failure",
                provider="OpenRouterModelProvider",
                model_id="google/gemini-3.7-flash",
                automatic_retry_allowed=False,
            )
        sources = kwargs["input_data"]["retrieval_context"]["sources"][:3]
        return {
            "general_opinion": {
                "general_opinion_id": "opinion_cycle030",
                "statement": "A saved-retrieval grounded general opinion.",
                "confidence": 0.8,
                "evidence_summary": "Three persisted sources support the summary.",
                "supporting_sources": [
                    {
                        "source_id": item["source_id"],
                    }
                    for item in sources
                ],
            }
        }


def build_cycle031_case(data_dir: Path):
    workflow_id = str(uuid4())
    retrieval_provider = MockRetrievalProvider(
        reservation_root=data_dir / "retrieval_call_reservations"
    )
    coordinator = RetrievalCoordinator(
        retrieval_provider,
        data_dir=data_dir,
        demo_safe_mode=True,
    )
    model_provider = OneCallGeneralOpinionProvider(data_dir)
    rd_loader = RoleDefinitionLoader.from_project(
        BASE_DIR,
        access_log_path=data_dir / "logs" / "rd_access.jsonl",
    )
    registry = ProducerRegistry(
        model_provider,
        {"producer.general_opinion_analyst": "google/gemini-3.7-flash"},
        rd_loader=rd_loader,
        demo_safe_mode=True,
        retrieval_coordinator=coordinator,
    )
    repository = WorkflowRepository(data_dir)
    manager = ProducerManager(
        registry,
        repository,
        rd_loader=rd_loader,
        demo_safe_mode=True,
    )
    selected_topic = {
        "topic_id": "topic_cycle031",
        "title": "Generative AI and copyright",
        "selection_reason": "saved selection",
    }
    agent_id = "producer.general_opinion_analyst"
    original_reservation = manager.provider_retry_store.reservation_path(
        provider_id="openrouter",
        workflow_id=workflow_id,
        task_id=agent_id,
    )
    original_reservation.parent.mkdir(parents=True, exist_ok=True)
    original_reservation.write_text(
        json.dumps(
            {
                "workflow_id": workflow_id,
                "task_id": agent_id,
                "agent_id": agent_id,
                "provider": "OpenRouterModelProvider",
                "model_id": "google/gemini-3.7-flash",
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    retry_authorization = manager.provider_retry_store.authorize_once(
        workflow_id=workflow_id,
        provider_id="openrouter",
        agent_id=agent_id,
        original_task_id=agent_id,
        source_error_message_id="cycle030_request_schema_error",
        source_error_class="ProviderRequestSchemaError",
    )
    retry_reservation = manager.provider_retry_store.reservation_path(
        provider_id="openrouter",
        workflow_id=workflow_id,
        task_id=retry_authorization.retry_task_id,
    )
    retry_reservation.write_text(
        json.dumps(
            {
                "workflow_id": workflow_id,
                "task_id": retry_authorization.retry_task_id,
                "agent_id": agent_id,
                "provider": "OpenRouterModelProvider",
                "model_id": "google/gemini-3.7-flash",
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    manager.provider_retry_store.consume(
        retry_authorization,
        reservation_path=retry_reservation,
    )
    retry_request = PMPMessage.create(
        workflow_id=workflow_id,
        sender_agent_id="producer.manager",
        receiver_agent_id=agent_id,
        message_type=MessageType.TASK,
        objective="Execute Cycle 030 retry",
        payload={"selected_topic": selected_topic, "revision_context": None},
        context=PMPContext(
            current_stage=agent_id,
            previous_stage="producer.topic_selector",
            next_stage="producer.research_planner",
        ),
        metadata=PMPMetadata(
            status=MessageStatus.QUEUED,
            extensions={
                "provider_task_id": retry_authorization.retry_task_id,
            },
        ),
    )
    retry_error = PMPMessage.create(
        workflow_id=workflow_id,
        parent_message_id=retry_request.message_id,
        sender_agent_id=agent_id,
        receiver_agent_id="producer.manager",
        message_type=MessageType.ERROR,
        objective="saved output hydration error",
        payload={
            "message": (
                "OUTPUT_CONTRACT_ERROR: General Opinion contains source references "
                "absent from or changed relative to retrieval context: source_cycle031_0"
            ),
            "task_id": retry_authorization.retry_task_id,
            "agent_id": agent_id,
            "provider": "OpenRouterModelProvider",
            "model_id": "google/gemini-3.7-flash",
            "error_class": "NonRetryableAgentError",
            "http_status": None,
        },
        metadata=PMPMetadata(status=MessageStatus.FAILED),
    )
    state = ProducerWorkflowState(
        workflow_id=workflow_id,
        status="FAILED",
        initial_request={"topic": "", "search_constraints": {}},
        topic_candidates=[{"topic_id": "topic_cycle031"}],
        selected_topic=selected_topic,
        completed_agents=["producer.topic_scout", "producer.topic_selector"],
        message_history=[retry_request, retry_error],
        role_definition_usage=[rd_loader.load("producer.manager").trace()],
        error={"message": "saved output hydration failure"},
        completed_at=datetime.now(timezone.utc),
    )
    repository.save(state)

    revision_hash = hashlib.sha256(b"null").hexdigest()[:8]
    retrieval_task_id = f"general_opinion_topic_cycle031_{revision_hash}"
    retrieval_id = RetrievalCoordinator._retrieval_id(
        workflow_id,
        retrieval_task_id,
        agent_id,
        RetrievalStrategy.GENERAL_OPINION.value,
    )
    now = datetime.now(timezone.utc)
    context = RetrievedContext(
        retrieval_id=retrieval_id,
        workflow_id=workflow_id,
        task_id=retrieval_task_id,
        research_question_id=None,
        agent_id=agent_id,
        retrieval_strategy=RetrievalStrategy.GENERAL_OPINION,
        queries=["saved query"],
        sources=[
            RetrievedSource(
                source_id=f"source_cycle031_{index}",
                url=f"https://example.invalid/cycle031/{index}",
                title=("long PDF title " + "x" * 1_801)
                if index == 0
                else f"saved source {index}",
                content=f"saved content {index}",
                rank=index + 1,
                query="saved query",
                provider_id="openrouter_web_search",
                retrieved_at=now,
            )
            for index in range(5)
        ],
        limitations=[],
        retrieved_at=now,
    )
    context_path = (
        data_dir / "retrieval_contexts" / workflow_id / f"{retrieval_id}.json"
    )
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(context.model_dump_json(indent=2), encoding="utf-8")
    retrieval_reservation = (
        data_dir
        / "retrieval_call_reservations"
        / "openrouter_web_search"
        / workflow_id
        / f"{retrieval_id}.json"
    )
    retrieval_reservation.parent.mkdir(parents=True, exist_ok=True)
    retrieval_reservation.write_text(
        json.dumps({"retrieval_id": retrieval_id, "workflow_id": workflow_id}),
        encoding="utf-8",
    )
    return (
        workflow_id,
        manager,
        repository,
        model_provider,
        retrieval_provider,
        context,
        context_path,
        retrieval_reservation,
    )


class ProducerCheckpointRecoveryTests(unittest.TestCase):
    def test_terminal_batch_retrieval_retries_once_with_new_sync_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            workflow_id = str(uuid4())
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            retrieval_provider.provider_id = "openrouter_web_search"
            retrieval_provider.model = "google/gemini-3.7-flash"
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            model_provider = OneCallGeneralOpinionProvider(data_dir)
            rd_loader = RoleDefinitionLoader.from_project(
                BASE_DIR,
                access_log_path=data_dir / "logs" / "rd_access.jsonl",
            )
            registry = ProducerRegistry(
                model_provider,
                {"producer.general_opinion_analyst": "google/gemini-3.7-flash"},
                rd_loader=rd_loader,
                demo_safe_mode=True,
                retrieval_coordinator=coordinator,
            )
            repository = WorkflowRepository(data_dir)
            manager = ProducerManager(
                registry,
                repository,
                rd_loader=rd_loader,
                demo_safe_mode=True,
            )
            selected_topic = {
                "topic_id": "topic_batch_retrieval",
                "title": "Generative AI and work",
                "selection_reason": "saved selection",
            }
            request = PMPMessage.create(
                workflow_id=workflow_id,
                sender_agent_id="producer.manager",
                receiver_agent_id="producer.general_opinion_analyst",
                message_type=MessageType.TASK,
                objective="saved Batch Retrieval request",
                payload={"selected_topic": selected_topic, "revision_context": None},
            )
            error = PMPMessage.create(
                workflow_id=workflow_id,
                parent_message_id=request.message_id,
                sender_agent_id="producer.general_opinion_analyst",
                receiver_agent_id="producer.manager",
                message_type=MessageType.ERROR,
                objective="saved terminal Batch Retrieval failure",
                payload={
                    "message": "OPENROUTER_BATCH_FAILED: terminal status=failed",
                    "task_id": "producer.general_opinion_analyst",
                    "agent_id": "producer.general_opinion_analyst",
                    "provider": "openrouter",
                    "model_id": "google/gemini-3.7-flash:batch",
                    "error_class": "NonRetryableAgentError",
                    "http_status": None,
                },
                metadata=PMPMetadata(status=MessageStatus.FAILED),
            )
            repository.save(
                ProducerWorkflowState(
                    workflow_id=workflow_id,
                    status="FAILED",
                    initial_request={"topic": "", "search_constraints": {}},
                    topic_candidates=[{"topic_id": "topic_batch_retrieval"}],
                    selected_topic=selected_topic,
                    completed_agents=[
                        "producer.topic_scout",
                        "producer.topic_selector",
                    ],
                    message_history=[request, error],
                    role_definition_usage=[
                        rd_loader.load("producer.manager").trace()
                    ],
                    error={"message": "saved terminal Batch Retrieval failure"},
                )
            )
            revision_hash = hashlib.sha256(b"null").hexdigest()[:8]
            original_task_id = (
                f"general_opinion_topic_batch_retrieval_{revision_hash}"
            )
            original_retrieval_id = coordinator.retrieval_identity(
                workflow_id=workflow_id,
                task_id=original_task_id,
                agent_id="producer.general_opinion_analyst",
                strategy=RetrievalStrategy.GENERAL_OPINION,
            )
            original_reservation = (
                data_dir
                / "retrieval_call_reservations"
                / "openrouter_web_search"
                / workflow_id
                / f"{original_retrieval_id}.json"
            )
            original_reservation.parent.mkdir(parents=True, exist_ok=True)
            original_reservation.write_text(
                json.dumps(
                    {
                        "retrieval_id": original_retrieval_id,
                        "workflow_id": workflow_id,
                        "task_id": original_task_id,
                        "agent_id": "producer.general_opinion_analyst",
                        "strategy": RetrievalStrategy.GENERAL_OPINION.value,
                        "provider_id": "openrouter_web_search",
                    }
                ),
                encoding="utf-8",
            )
            sidecar = (
                data_dir
                / "openrouter_batch_jobs"
                / "retrieval_call_reservations"
                / "openrouter_web_search"
                / workflow_id
                / f"{original_retrieval_id}.0123456789abcdef.json"
            )
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(
                json.dumps(
                    {
                        "model_id": "google/gemini-3.7-flash:batch",
                        "batch_id": "batch-terminal",
                        "status": "failed",
                    }
                ),
                encoding="utf-8",
            )

            recovered = asyncio.run(manager.retry_retrieval_provider(workflow_id))

            self.assertEqual(recovered.status, "RUNNING")
            self.assertEqual(recovered.completed_agents[-1], "producer.general_opinion_analyst")
            self.assertEqual(retrieval_provider.calls, 1)
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])
            authorization = manager.retrieval_provider_retry_store.for_original_task(
                workflow_id=workflow_id,
                retrieval_provider_id="openrouter_web_search",
                original_task_id=original_task_id,
            )
            self.assertIsNotNone(authorization)
            self.assertEqual(
                authorization.status,
                RetrievalProviderRetryStatus.CONSUMED.value,
            )
            self.assertIsNotNone(authorization.retrieval_context_sha256)
            with self.assertRaisesRegex(ValueError, "must be FAILED"):
                asyncio.run(manager.retry_retrieval_provider(workflow_id))
            self.assertEqual(retrieval_provider.calls, 1)
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])

    def test_batch_endpoint_404_can_authorize_one_code_repair_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            workflow_id = str(uuid4())
            model_provider = OneCallGeneralOpinionProvider(data_dir)
            rd_loader = RoleDefinitionLoader.from_project(
                BASE_DIR,
                access_log_path=data_dir / "logs" / "rd_access.jsonl",
            )
            registry = ProducerRegistry(
                model_provider,
                {
                    "producer.topic_scout": (
                        "google/gemini-3.7-flash:batch"
                    )
                },
                rd_loader=rd_loader,
                demo_safe_mode=True,
                retrieval_coordinator=RetrievalCoordinator(
                    MockRetrievalProvider(
                        reservation_root=data_dir / "retrieval_call_reservations"
                    ),
                    data_dir=data_dir,
                    demo_safe_mode=True,
                ),
            )
            repository = WorkflowRepository(data_dir)
            manager = ProducerManager(
                registry,
                repository,
                rd_loader=rd_loader,
                demo_safe_mode=True,
            )
            request = PMPMessage.create(
                workflow_id=workflow_id,
                sender_agent_id="producer.manager",
                receiver_agent_id="producer.topic_scout",
                message_type=MessageType.TASK,
                objective="saved batch transport request",
                payload={"topic": "batch", "search_constraints": {}},
            )
            error = PMPMessage.create(
                workflow_id=workflow_id,
                parent_message_id=request.message_id,
                sender_agent_id="producer.topic_scout",
                receiver_agent_id="producer.manager",
                message_type=MessageType.ERROR,
                objective="saved batch transport error",
                payload={
                    "message": (
                        "OpenRouter HTTP 404: This model is only available through the "
                        "Batch API. Use the /api/beta/batches endpoint instead."
                    ),
                    "task_id": "producer.topic_scout",
                    "agent_id": "producer.topic_scout",
                    "provider": "openrouter",
                    "model_id": "google/gemini-3.7-flash:batch",
                    "error_class": "NonRetryableAgentError",
                    "http_status": 404,
                },
                metadata=PMPMetadata(status=MessageStatus.FAILED),
            )
            repository.save(
                ProducerWorkflowState(
                    workflow_id=workflow_id,
                    status="FAILED",
                    initial_request={"topic": "", "search_constraints": {}},
                    completed_agents=[],
                    message_history=[request, error],
                    error={"message": "saved batch endpoint mismatch"},
                )
            )
            reservation = manager.provider_retry_store.reservation_path(
                provider_id="openrouter",
                workflow_id=workflow_id,
                task_id="producer.topic_scout",
            )
            reservation.parent.mkdir(parents=True)
            reservation.write_text(
                json.dumps(
                    {
                        "workflow_id": workflow_id,
                        "task_id": "producer.topic_scout",
                        "agent_id": "producer.topic_scout",
                    }
                ),
                encoding="utf-8",
            )

            authorization = manager.authorize_provider_retry(workflow_id)

            self.assertEqual(authorization.status, ProviderRetryStatus.PENDING.value)
            self.assertEqual(
                authorization.retry_task_id,
                "producer.topic_scout_operator_retry_1",
            )

    def test_terminal_failed_batch_can_authorize_one_new_task_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            workflow_id = str(uuid4())
            model_provider = OneCallGeneralOpinionProvider(data_dir)
            model_provider.allow_failed_batch_retry = True
            rd_loader = RoleDefinitionLoader.from_project(
                BASE_DIR,
                access_log_path=data_dir / "logs" / "rd_access.jsonl",
            )
            registry = ProducerRegistry(
                model_provider,
                {"producer.topic_scout": "google/gemini-3.7-flash:batch"},
                rd_loader=rd_loader,
                demo_safe_mode=True,
                retrieval_coordinator=RetrievalCoordinator(
                    MockRetrievalProvider(
                        reservation_root=data_dir / "retrieval_call_reservations"
                    ),
                    data_dir=data_dir,
                    demo_safe_mode=True,
                ),
            )
            repository = WorkflowRepository(data_dir)
            manager = ProducerManager(
                registry,
                repository,
                rd_loader=rd_loader,
                demo_safe_mode=True,
            )
            request = PMPMessage.create(
                workflow_id=workflow_id,
                sender_agent_id="producer.manager",
                receiver_agent_id="producer.topic_scout",
                message_type=MessageType.TASK,
                objective="saved native batch request",
                payload={"topic": "batch", "search_constraints": {}},
            )
            error = PMPMessage.create(
                workflow_id=workflow_id,
                parent_message_id=request.message_id,
                sender_agent_id="producer.topic_scout",
                receiver_agent_id="producer.manager",
                message_type=MessageType.ERROR,
                objective="saved native batch failure",
                payload={
                    "message": "OPENROUTER_BATCH_FAILED: terminal status=failed",
                    "task_id": "producer.topic_scout",
                    "agent_id": "producer.topic_scout",
                    "provider": "openrouter",
                    "model_id": "google/gemini-3.7-flash:batch",
                    "error_class": "NonRetryableAgentError",
                    "http_status": None,
                },
                metadata=PMPMetadata(status=MessageStatus.FAILED),
            )
            repository.save(
                ProducerWorkflowState(
                    workflow_id=workflow_id,
                    status="FAILED",
                    initial_request={"topic": "", "search_constraints": {}},
                    completed_agents=[],
                    message_history=[request, error],
                    error={"message": "saved terminal batch failure"},
                )
            )
            reservation = manager.provider_retry_store.reservation_path(
                provider_id="openrouter",
                workflow_id=workflow_id,
                task_id="producer.topic_scout",
            )
            reservation.parent.mkdir(parents=True)
            reservation.write_text(
                json.dumps(
                    {
                        "workflow_id": workflow_id,
                        "task_id": "producer.topic_scout",
                        "agent_id": "producer.topic_scout",
                    }
                ),
                encoding="utf-8",
            )

            authorization = manager.authorize_provider_retry(workflow_id)

            self.assertEqual(authorization.status, ProviderRetryStatus.PENDING.value)
            self.assertEqual(
                authorization.retry_task_id,
                "producer.topic_scout_operator_retry_1",
            )
            self.assertEqual(
                authorization.source_error_class,
                "ProviderRequestSchemaError",
            )

    def test_one_time_reasoning_retry_reuses_retrieval_and_stops_downstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            workflow_id = str(uuid4())
            retrieval_provider = MockRetrievalProvider(
                reservation_root=data_dir / "retrieval_call_reservations"
            )
            coordinator = RetrievalCoordinator(
                retrieval_provider,
                data_dir=data_dir,
                demo_safe_mode=True,
            )
            model_provider = OneCallGeneralOpinionProvider(data_dir)
            rd_loader = RoleDefinitionLoader.from_project(
                BASE_DIR,
                access_log_path=data_dir / "logs" / "rd_access.jsonl",
            )
            registry = ProducerRegistry(
                model_provider,
                {"producer.general_opinion_analyst": "google/gemini-3.7-flash"},
                rd_loader=rd_loader,
                demo_safe_mode=True,
                retrieval_coordinator=coordinator,
            )
            repository = WorkflowRepository(data_dir)
            manager = ProducerManager(
                registry,
                repository,
                rd_loader=rd_loader,
                demo_safe_mode=True,
            )

            selected_topic = {
                "topic_id": "topic_cycle030",
                "title": "Generative AI and copyright",
                "selection_reason": "saved selection",
            }
            task_request = PMPMessage.create(
                workflow_id=workflow_id,
                sender_agent_id="producer.manager",
                receiver_agent_id="producer.general_opinion_analyst",
                message_type=MessageType.TASK,
                objective="Execute saved General Opinion checkpoint",
                payload={"selected_topic": selected_topic, "revision_context": None},
                context=PMPContext(
                    current_stage="producer.general_opinion_analyst",
                    previous_stage="producer.topic_selector",
                    next_stage="producer.research_planner",
                ),
            )
            error_response = PMPMessage.create(
                workflow_id=workflow_id,
                parent_message_id=task_request.message_id,
                sender_agent_id="producer.general_opinion_analyst",
                receiver_agent_id="producer.manager",
                message_type=MessageType.ERROR,
                objective="saved request schema error",
                payload={
                    "message": (
                        "OpenRouter HTTP 400: Google AI Studio INVALID_ARGUMENT: "
                        "Request contains an invalid argument."
                    ),
                    "task_id": None,
                    "agent_id": "producer.general_opinion_analyst",
                    "provider": "openrouter",
                    "model_id": "google/gemini-3.7-flash",
                    "error_class": "NonRetryableAgentError",
                    "http_status": 400,
                },
                metadata=PMPMetadata(status=MessageStatus.FAILED),
            )
            manager_trace = rd_loader.load("producer.manager").trace()
            state = ProducerWorkflowState(
                workflow_id=workflow_id,
                status="FAILED",
                initial_request={"topic": "", "search_constraints": {}},
                topic_candidates=[{"topic_id": "topic_cycle030"}],
                selected_topic=selected_topic,
                completed_agents=[
                    "producer.topic_scout",
                    "producer.topic_selector",
                ],
                message_history=[task_request, error_response],
                role_definition_usage=[manager_trace],
                error={"message": "saved Gemini INVALID_ARGUMENT"},
                completed_at=datetime.now(timezone.utc),
            )
            repository.save(state)

            original_reservation = (
                data_dir
                / "provider_call_reservations"
                / "openrouter"
                / workflow_id
                / "producer.general_opinion_analyst.json"
            )
            original_reservation.parent.mkdir(parents=True, exist_ok=True)
            original_reservation.write_text(
                json.dumps(
                    {
                        "workflow_id": workflow_id,
                        "task_id": "producer.general_opinion_analyst",
                        "agent_id": "producer.general_opinion_analyst",
                        "provider": "OpenRouterModelProvider",
                        "model_id": "google/gemini-3.7-flash",
                        "reserved_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                encoding="utf-8",
            )

            revision_hash = hashlib.sha256(b"null").hexdigest()[:8]
            retrieval_task_id = f"general_opinion_topic_cycle030_{revision_hash}"
            retrieval_id = RetrievalCoordinator._retrieval_id(
                workflow_id,
                retrieval_task_id,
                "producer.general_opinion_analyst",
                RetrievalStrategy.GENERAL_OPINION.value,
            )
            now = datetime.now(timezone.utc)
            context = RetrievedContext(
                retrieval_id=retrieval_id,
                workflow_id=workflow_id,
                task_id=retrieval_task_id,
                research_question_id=None,
                agent_id="producer.general_opinion_analyst",
                retrieval_strategy=RetrievalStrategy.GENERAL_OPINION,
                queries=["saved query"],
                sources=[
                    RetrievedSource(
                        source_id=f"source_cycle030_{index}",
                        url=f"https://example.invalid/cycle030/{index}",
                        title=("long PDF title " + "x" * 1_801)
                        if index == 0
                        else f"saved source {index}",
                        content=f"saved content {index}",
                        rank=index + 1,
                        query="saved query",
                        provider_id="openrouter_web_search",
                        retrieved_at=now,
                    )
                    for index in range(5)
                ],
                limitations=[],
                retrieved_at=now,
            )
            context_path = (
                data_dir
                / "retrieval_contexts"
                / workflow_id
                / f"{retrieval_id}.json"
            )
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text(context.model_dump_json(indent=2), encoding="utf-8")
            context_hash_before = hashlib.sha256(context_path.read_bytes()).hexdigest()

            recovered = asyncio.run(manager.retry_provider_call(workflow_id))

            self.assertEqual(recovered.status, "RUNNING")
            self.assertEqual(
                recovered.completed_agents,
                [
                    "producer.topic_scout",
                    "producer.topic_selector",
                    "producer.general_opinion_analyst",
                ],
            )
            self.assertIsNotNone(recovered.general_opinion)
            self.assertEqual(
                recovered.general_opinion["supporting_sources"][0]["source"],
                context.sources[0].title,
            )
            self.assertEqual(
                str(recovered.general_opinion["supporting_sources"][0]["url"]),
                str(context.sources[0].url),
            )
            self.assertIsNone(recovered.research_plan)
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])
            self.assertEqual(retrieval_provider.calls, 0)
            self.assertEqual(
                hashlib.sha256(context_path.read_bytes()).hexdigest(),
                context_hash_before,
            )

            authorization = manager.provider_retry_store.for_original_task(
                workflow_id=workflow_id,
                provider_id="openrouter",
                original_task_id="producer.general_opinion_analyst",
            )
            self.assertIsNotNone(authorization)
            self.assertEqual(authorization.status, ProviderRetryStatus.CONSUMED.value)
            retry_reservation = manager.provider_retry_store.reservation_path(
                provider_id="openrouter",
                workflow_id=workflow_id,
                task_id=authorization.retry_task_id,
            )
            self.assertTrue(retry_reservation.exists())

            with self.assertRaisesRegex(ValueError, "must be FAILED"):
                asyncio.run(manager.retry_provider_call(workflow_id))
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])

    def test_cycle031_output_repair_is_one_shot_and_retrieval_free(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (
                workflow_id,
                manager,
                _repository,
                model_provider,
                retrieval_provider,
                context,
                context_path,
                retrieval_reservation,
            ) = build_cycle031_case(data_dir)
            context_hash_before = hashlib.sha256(context_path.read_bytes()).hexdigest()
            retrieval_reservation_hash_before = hashlib.sha256(
                retrieval_reservation.read_bytes()
            ).hexdigest()

            recovered = asyncio.run(manager.repair_provider_output(workflow_id))

            self.assertEqual(recovered.status, "RUNNING")
            self.assertEqual(
                recovered.completed_agents,
                [
                    "producer.topic_scout",
                    "producer.topic_selector",
                    "producer.general_opinion_analyst",
                ],
            )
            self.assertIsNotNone(recovered.general_opinion)
            self.assertIsNone(recovered.research_plan)
            self.assertEqual(
                recovered.general_opinion["supporting_sources"][0]["source"],
                context.sources[0].title,
            )
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])
            self.assertEqual(retrieval_provider.calls, 0)
            self.assertEqual(
                hashlib.sha256(context_path.read_bytes()).hexdigest(),
                context_hash_before,
            )
            self.assertEqual(
                hashlib.sha256(retrieval_reservation.read_bytes()).hexdigest(),
                retrieval_reservation_hash_before,
            )

            authorization = manager.provider_output_repair_store.for_original_task(
                workflow_id=workflow_id,
                provider_id="openrouter",
                original_task_id="producer.general_opinion_analyst",
            )
            self.assertIsNotNone(authorization)
            self.assertEqual(
                authorization.status,
                ProviderOutputRepairStatus.CONSUMED.value,
            )
            self.assertEqual(
                authorization.retrieval_context_sha256,
                context_hash_before,
            )
            self.assertEqual(
                authorization.repair_task_id,
                "producer.general_opinion_analyst_provider_output_repair_1",
            )
            repair_reservation = manager.provider_output_repair_store.reservation_path(
                provider_id="openrouter",
                workflow_id=workflow_id,
                task_id=authorization.repair_task_id,
            )
            self.assertTrue(repair_reservation.exists())
            with self.assertRaisesRegex(ValueError, "must be FAILED"):
                asyncio.run(manager.repair_provider_output(workflow_id))
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])

    def test_cycle031_changed_retrieval_hash_blocks_before_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (
                workflow_id,
                manager,
                _repository,
                model_provider,
                retrieval_provider,
                _context,
                context_path,
                _retrieval_reservation,
            ) = build_cycle031_case(data_dir)
            manager.authorize_provider_output_repair(workflow_id)
            context_path.write_text(
                context_path.read_text(encoding="utf-8") + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "different identity|hash changed",
            ):
                asyncio.run(manager.repair_provider_output(workflow_id))
            self.assertEqual(model_provider.calls, [])
            self.assertEqual(retrieval_provider.calls, 0)

    def test_cycle031_failed_call_consumes_identity_and_cannot_call_again(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            (
                workflow_id,
                manager,
                _repository,
                model_provider,
                retrieval_provider,
                _context,
                _context_path,
                _retrieval_reservation,
            ) = build_cycle031_case(data_dir)
            model_provider.fail = True

            failed = asyncio.run(manager.repair_provider_output(workflow_id))

            self.assertEqual(failed.status, "FAILED")
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])
            authorization = manager.provider_output_repair_store.for_original_task(
                workflow_id=workflow_id,
                provider_id="openrouter",
                original_task_id="producer.general_opinion_analyst",
            )
            self.assertEqual(
                authorization.status,
                ProviderOutputRepairStatus.CONSUMED.value,
            )
            with self.assertRaisesRegex(ValueError, "already consumed"):
                asyncio.run(manager.repair_provider_output(workflow_id))
            self.assertEqual(model_provider.calls, ["GeneralOpinionOutput"])
            self.assertEqual(retrieval_provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
