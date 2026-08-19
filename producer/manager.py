from __future__ import annotations

import hashlib
import inspect
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from common.ids import new_workflow_id
from common.models.pmp import (
    MessageStatus,
    MessageType,
    PMPContext,
    PMPMessage,
    PMPMetadata,
    PMPRouting,
)
from common.models.workflow import WorkflowStatus
from common.provider_retry import (
    ProviderRetryAuthorization,
    ProviderRetryAuthorizationStore,
    ProviderRetryStatus,
)
from common.provider_output_repair import (
    ProviderOutputRepairAuthorization,
    ProviderOutputRepairAuthorizationStore,
)
from common.validation import PMPValidator
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from producer.registry import ProducerRegistry
from producer.schemas.review import QualityReviewOutput
from producer.state import ProducerWorkflowState, RevisionRecord, utc_now
from producer.workflow import AGENT_ORDER, DISPLAY_NAMES
from retrieval.models import RetrievedContext
from storage.workflow_repository import WorkflowRepository


ProgressCallback = Callable[[str], Awaitable[None] | None]


class ProducerManager:
    agent_id = "producer.manager"

    def __init__(
        self,
        registry: ProducerRegistry,
        repository: WorkflowRepository,
        *,
        max_revisions: int = 3,
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
    ) -> None:
        self.registry = registry
        self.repository = repository
        if (
            getattr(self.registry.provider, "reservation_root", None) is None
            and not os.getenv("PRDCP_DATA_DIR", "").strip()
        ):
            self.registry.provider.reservation_root = (
                repository.data_dir / "provider_call_reservations"
            )
        self.registry.bind_retrieval_data_dir(repository.data_dir)
        self.demo_safe_mode = demo_safe_mode
        self.max_revisions = 0 if demo_safe_mode else max_revisions
        self.pmp_validator = PMPValidator()
        self.rd_loader = rd_loader or registry.rd_loader
        self.provider_retry_store = ProviderRetryAuthorizationStore(repository.data_dir)
        self.provider_output_repair_store = ProviderOutputRepairAuthorizationStore(
            repository.data_dir
        )

    def authorize_provider_retry(
        self,
        workflow_id: str,
    ) -> ProviderRetryAuthorization:
        """Authorize one repaired General Opinion reasoning request.

        Cycle 030 migrates the already-persisted generic HTTP 400 into the
        precise request-schema failure classification only after correlating
        the PMP error, original request, agent, endpoint and reservation.
        """

        if not self.demo_safe_mode:
            raise ValueError(
                "Operator provider retry is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError("Producer must be FAILED before an operator provider retry")
        next_index = self._recovery_start_index(state)
        agent_id = AGENT_ORDER[next_index]
        if agent_id != "producer.general_opinion_analyst":
            raise ValueError(
                "Cycle 030 provider retry is limited to the failed General Opinion checkpoint"
            )
        error_response = next(
            (
                message
                for message in reversed(state.message_history)
                if message.sender_agent_id == agent_id
                and message.receiver_agent_id == self.agent_id
                and message.message_type == MessageType.ERROR.value
            ),
            None,
        )
        if error_response is None:
            raise ValueError("No persisted General Opinion error was found")
        request = next(
            (
                message
                for message in reversed(state.message_history)
                if message.message_id == error_response.parent_message_id
            ),
            None,
        )
        error_message = str(error_response.payload.get("message") or "")
        if (
            request is None
            or request.sender_agent_id != self.agent_id
            or request.receiver_agent_id != agent_id
            or request.message_type != MessageType.TASK.value
            or error_response.payload.get("provider") != "openrouter"
            or error_response.payload.get("http_status") != 400
            or "INVALID_ARGUMENT" not in error_message
        ):
            raise ValueError(
                "Persisted General Opinion failure is not the correlated Gemini "
                "request-schema rejection"
            )
        provider_id = getattr(self.registry.get(agent_id).provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("General Opinion provider has no stable logical provider ID")
        original_task_id = str(error_response.payload.get("task_id") or agent_id)
        return self.provider_retry_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            source_error_class="ProviderRequestSchemaError",
        )

    async def retry_provider_call(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ProducerWorkflowState:
        """Run exactly the failed reasoning checkpoint, never downstream agents."""

        authorization = self.authorize_provider_retry(workflow_id)
        await self._emit(
            progress_callback,
            "Operator one-time General Opinion retry authorized: "
            + authorization.retry_task_id,
        )
        return await self.recover(
            workflow_id,
            progress_callback=progress_callback,
            stop_after_checkpoint=True,
        )

    def authorize_provider_output_repair(
        self,
        workflow_id: str,
    ) -> ProviderOutputRepairAuthorization:
        """Authorize the post-Cycle-030 output hydration repair exactly once."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Provider output repair is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError("Producer must be FAILED before a provider output repair")
        start_index = self._recovery_start_index(state)
        agent_id = AGENT_ORDER[start_index]
        if agent_id != "producer.general_opinion_analyst":
            raise ValueError(
                "Provider output repair is limited to the failed General Opinion checkpoint"
            )
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if provider_id != "openrouter":
            raise ValueError("General Opinion output repair requires OpenRouter")

        original_task_id = agent_id
        prior_authorization = self.provider_retry_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if (
            prior_authorization is None
            or prior_authorization.status != ProviderRetryStatus.CONSUMED.value
        ):
            raise ValueError(
                "Provider output repair requires the consumed Cycle 030 retry authorization"
            )
        existing_repair = self.provider_output_repair_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if existing_repair is not None:
            if existing_repair.status != "PENDING":
                raise ValueError(
                    "The one-time provider output repair was already consumed"
                )
            return self.provider_output_repair_store.require_pending_repair(
                workflow_id=workflow_id,
                provider_id=provider_id,
                agent_id=agent_id,
                repair_task_id=existing_repair.repair_task_id,
                model_id=agent.model,
            )
        error_response = next(
            (
                message
                for message in reversed(state.message_history)
                if message.sender_agent_id == agent_id
                and message.receiver_agent_id == self.agent_id
                and message.message_type == MessageType.ERROR.value
            ),
            None,
        )
        if error_response is None:
            raise ValueError("No persisted General Opinion output error was found")
        request = next(
            (
                message
                for message in reversed(state.message_history)
                if message.message_id == error_response.parent_message_id
            ),
            None,
        )
        error_message = str(error_response.payload.get("message") or "")
        failed_task_id = str(error_response.payload.get("task_id") or "")
        failed_model_id = str(error_response.payload.get("model_id") or "")
        if (
            request is None
            or request.sender_agent_id != self.agent_id
            or request.receiver_agent_id != agent_id
            or request.message_type != MessageType.TASK.value
            or request.metadata.extensions.get("provider_task_id")
            != prior_authorization.retry_task_id
            or failed_task_id != prior_authorization.retry_task_id
            or error_response.payload.get("error_class")
            != "NonRetryableAgentError"
            or not error_message.startswith(
                "OUTPUT_CONTRACT_ERROR: General Opinion contains source references "
                "absent from or changed relative to retrieval context:"
            )
            or failed_model_id != agent.model
        ):
            raise ValueError(
                "Persisted failure is not the correlated Cycle 030 Retrieval metadata "
                "hydration contract failure"
            )

        context_root = (
            self.repository.data_dir / "retrieval_contexts" / workflow_id
        )
        contexts: list[tuple[RetrievedContext, Path]] = []
        if context_root.is_dir():
            for path in sorted(context_root.glob("*.json")):
                context = RetrievedContext.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if (
                    context.workflow_id == workflow_id
                    and context.agent_id == agent_id
                ):
                    contexts.append((context, path))
        if len(contexts) != 1:
            raise ValueError(
                "Provider output repair requires exactly one saved General Opinion "
                "Retrieval Context"
            )
        context, context_path = contexts[0]
        if len(context.sources) < 3:
            raise ValueError(
                "Saved General Opinion Retrieval Context no longer has enough sources"
            )
        context_sha256 = hashlib.sha256(context_path.read_bytes()).hexdigest()
        return self.provider_output_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            source_error_class="NonRetryableAgentError",
            model_id=failed_model_id,
            retrieval_id=context.retrieval_id,
            retrieval_context_sha256=context_sha256,
        )

    async def repair_provider_output(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ProducerWorkflowState:
        """Run only General Opinion reasoning with the repaired output adapter."""

        authorization = self.authorize_provider_output_repair(workflow_id)
        await self._emit(
            progress_callback,
            "One-shot General Opinion output repair authorized: "
            + authorization.repair_task_id,
        )
        state = self.repository.load(workflow_id)
        start_index = self._recovery_start_index(state)
        state.error = None
        state.completed_at = None
        self.repository.save(state)
        return await self._run_from(
            state,
            start_index,
            progress_callback,
            provider_task_id_overrides={
                "producer.general_opinion_analyst": authorization.repair_task_id
            },
            stop_after_index=start_index,
        )

    async def recover(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
        stop_after_checkpoint: bool = False,
    ) -> ProducerWorkflowState:
        """Resume from the first incomplete Producer checkpoint.

        A previously reserved failed Provider task requires a pending one-shot
        authorization.  Saved Retrieval is resolved by its canonical identity,
        so recovering reasoning cannot start another search.
        """

        state = self.repository.load(workflow_id)
        if state.status == WorkflowStatus.COMPLETED.value:
            return state
        start_index = self._recovery_start_index(state)
        agent_id = AGENT_ORDER[start_index]
        provider_id = getattr(self.registry.get(agent_id).provider, "provider_id", None)
        task_overrides: dict[str, str] = {}
        if isinstance(provider_id, str):
            authorization = self.provider_retry_store.for_original_task(
                workflow_id=workflow_id,
                provider_id=provider_id,
                original_task_id=agent_id,
            )
            original_reservation = self.provider_retry_store.reservation_path(
                provider_id=provider_id,
                workflow_id=workflow_id,
                task_id=agent_id,
            )
            if (
                authorization is not None
                and authorization.status == ProviderRetryStatus.PENDING.value
            ):
                task_overrides[agent_id] = authorization.retry_task_id
            elif original_reservation.exists():
                raise ValueError(
                    "Producer recovery found a prior Provider reservation for the "
                    f"incomplete checkpoint {agent_id}; use --producer-provider-retry"
                )
        state.error = None
        state.completed_at = None
        self.repository.save(state)
        return await self._run_from(
            state,
            start_index,
            progress_callback,
            provider_task_id_overrides=task_overrides,
            stop_after_index=start_index if stop_after_checkpoint else None,
        )

    async def start(
        self,
        *,
        user_topic: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ProducerWorkflowState:
        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime_config = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime_config.revision_limit is not None:
            self.max_revisions = runtime_config.revision_limit
        workflow_id = new_workflow_id()
        state = ProducerWorkflowState(
            workflow_id=workflow_id,
            initial_request={
                "topic": user_topic or "",
                "user_request": "ユーザー指定Topicから開始" if user_topic else "話題候補を自動探索",
                "search_constraints": {
                    "sources": ["news", "sns", "youtube", "reddit"],
                    "language": "ja",
                    "max_candidates": 3,
                    "required_keywords": ["AI"],
                },
            },
            role_definition_usage=[manager_snapshot.trace()],
        )
        self.repository.save(state)
        await self._emit(progress_callback, f"Workflow開始: {workflow_id}")
        return await self._run_from(state, 0, progress_callback)

    async def _run_from(
        self,
        state: ProducerWorkflowState,
        start_index: int,
        progress_callback: ProgressCallback | None,
        *,
        provider_task_id_overrides: dict[str, str] | None = None,
        stop_after_index: int | None = None,
    ) -> ProducerWorkflowState:
        state.status = WorkflowStatus.RUNNING
        self.repository.save(state)
        for index in range(start_index, len(AGENT_ORDER)):
            agent_id = AGENT_ORDER[index]
            state.current_agent_id = agent_id
            state.status = (
                WorkflowStatus.REVIEWING
                if agent_id == "producer.quality_reviewer"
                else WorkflowStatus.WAITING_AGENT
            )
            self.repository.save(state)

            request = self._create_task_message(
                state,
                agent_id,
                index,
                provider_task_id=(provider_task_id_overrides or {}).get(agent_id),
            )
            state.message_history.append(request)
            self.repository.save(state)

            response = await self.registry.get(agent_id).execute(request)
            state.message_history.append(response)
            self.repository.save(state)

            error = self._validate_response(request, response, agent_id)
            if error:
                return await self._fail(state, error, progress_callback)

            if response.message_type == MessageType.REVIEW.value:
                return await self._handle_review(state, response, progress_callback)

            self._apply_result(state, agent_id, response.payload)
            self._mark_complete(state, agent_id)
            state.status = WorkflowStatus.RUNNING
            self.repository.save(state)
            await self._emit(
                progress_callback,
                f"[{index + 1}/5] {DISPLAY_NAMES[agent_id]} 完了",
            )
            if index == stop_after_index:
                state.current_agent_id = None
                state.status = WorkflowStatus.RUNNING
                state.error = None
                state.completed_at = None
                self.repository.save(state)
                await self._emit(
                    progress_callback,
                    "Recovered checkpoint saved; downstream Provider calls were not run",
                )
                return state
        return await self._fail(state, "Quality Reviewerを通過せずに処理が終了しました", progress_callback)

    def _create_task_message(
        self,
        state: ProducerWorkflowState,
        agent_id: str,
        index: int,
        *,
        provider_task_id: str | None = None,
    ) -> PMPMessage:
        previous_stage = AGENT_ORDER[index - 1] if index > 0 else "producer.manager"
        next_stage = AGENT_ORDER[index + 1] if index + 1 < len(AGENT_ORDER) else "producer.manager"
        return PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=agent_id,
            message_type=MessageType.TASK,
            objective=f"Execute {agent_id} in Producer workflow",
            payload=self._build_payload(state, agent_id),
            constraints={"do_not_judge_general_opinion_truth": True},
            context=PMPContext(
                current_stage=agent_id,
                previous_stage=previous_stage,
                next_stage=next_stage,
            ),
            metadata=PMPMetadata(
                status=MessageStatus.QUEUED,
                extensions={
                    "role_definition": state.role_definition_usage[-1],
                    **(
                        {"provider_task_id": provider_task_id}
                        if provider_task_id is not None
                        else {}
                    ),
                },
            ),
        )

    @staticmethod
    def _recovery_start_index(state: ProducerWorkflowState) -> int:
        completed = list(dict.fromkeys(state.completed_agents))
        expected_prefix = AGENT_ORDER[: len(completed)]
        if completed != expected_prefix:
            raise ValueError(
                "Producer checkpoint is not a contiguous completed-agent prefix"
            )
        if len(completed) >= len(AGENT_ORDER):
            raise ValueError("Producer has no incomplete checkpoint to recover")
        required_artifacts = (
            (1, bool(state.topic_candidates), "topic_candidates"),
            (2, state.selected_topic is not None, "selected_topic"),
            (3, state.general_opinion is not None, "general_opinion"),
            (4, state.research_plan is not None, "research_plan"),
        )
        for completed_count, present, name in required_artifacts:
            if len(completed) >= completed_count and not present:
                raise ValueError(
                    f"Producer checkpoint says {completed_count} agents completed but "
                    f"{name} is missing"
                )
        return len(completed)

    def _build_payload(self, state: ProducerWorkflowState, agent_id: str) -> dict[str, Any]:
        revision_context = (
            state.revision_history[-1].model_dump(mode="json") if state.revision_history else None
        )
        if agent_id == "producer.topic_scout":
            constraints = state.initial_request["search_constraints"]
            return {
                "search_query": state.initial_request["topic"],
                "search_sources": constraints["sources"],
                "search_constraints": {
                    "language": constraints["language"],
                    "max_candidates": constraints["max_candidates"],
                    "required_keywords": constraints["required_keywords"],
                },
                "user_topic": state.initial_request["topic"] or None,
                "revision_context": revision_context,
            }
        if agent_id == "producer.topic_selector":
            return {"topic_candidates": state.topic_candidates, "revision_context": revision_context}
        if agent_id == "producer.general_opinion_analyst":
            return {"selected_topic": state.selected_topic, "revision_context": revision_context}
        if agent_id == "producer.research_planner":
            return {
                "selected_topic": state.selected_topic,
                "general_opinion": state.general_opinion,
                "revision_context": revision_context,
            }
        if agent_id == "producer.quality_reviewer":
            return {"research_plan": state.research_plan, "revision_context": revision_context}
        raise KeyError(f"Unknown agent in workflow: {agent_id}")

    def _validate_response(self, request: PMPMessage, response: PMPMessage, agent_id: str) -> str | None:
        try:
            self.pmp_validator.validate(response)
        except Exception as exc:
            return f"Invalid PMP response from {agent_id}: {exc}"
        if response.workflow_id != request.workflow_id:
            return f"Workflow ID mismatch from {agent_id}"
        if response.parent_message_id != request.message_id:
            return f"Parent message ID mismatch from {agent_id}"
        if response.sender_agent_id != agent_id or response.receiver_agent_id != self.agent_id:
            return f"Invalid routing in response from {agent_id}"
        if response.message_type == MessageType.ERROR.value:
            return f"{agent_id}: {response.payload.get('message', 'unknown error')}"
        expected = MessageType.REVIEW.value if agent_id == "producer.quality_reviewer" else MessageType.RESULT.value
        if response.message_type != expected:
            return f"Unexpected message type from {agent_id}: {response.message_type}"
        return None

    def _apply_result(self, state: ProducerWorkflowState, agent_id: str, payload: dict) -> None:
        if agent_id == "producer.topic_scout":
            state.topic_candidates = payload["topic_candidates"]
        elif agent_id == "producer.topic_selector":
            state.selected_topic = payload["selected_topic"]
        elif agent_id == "producer.general_opinion_analyst":
            state.general_opinion = payload["general_opinion"]
        elif agent_id == "producer.research_planner":
            state.research_plan = payload["research_plan"]

    async def _handle_review(
        self,
        state: ProducerWorkflowState,
        response: PMPMessage,
        progress_callback: ProgressCallback | None,
    ) -> ProducerWorkflowState:
        review = QualityReviewOutput.model_validate(response.payload)
        state.review_result = review.model_dump(mode="json")
        status = review.status
        if status in {"approved", "approved_with_conditions"}:
            self._mark_complete(state, "producer.quality_reviewer")
            state.status = WorkflowStatus.APPROVED
            self.repository.save(state)
            await self._emit(progress_callback, "[5/5] Quality Reviewer: approved")
            try:
                self._send_to_researcher(state)
            except Exception as exc:
                return await self._fail(
                    state,
                    f"Researcher Inboxへの送信に失敗しました: {exc}",
                    progress_callback,
                )
            state.researcher_sent = True
            state.status = WorkflowStatus.COMPLETED
            state.current_agent_id = None
            state.completed_at = utc_now()
            self.repository.save(state)
            return state
        if status == "revision_required":
            if self.demo_safe_mode:
                return await self._fail(
                    state,
                    "Demo Safe Mode stopped automatic reviewer revision and Manager re-dispatch",
                    progress_callback,
                )
            state.revision_count += 1
            state.revision_history.append(
                RevisionRecord(
                    iteration=state.revision_count,
                    target_agent=review.revision_target or "",
                    reason=review.reason,
                    required_action=review.required_action or "",
                )
            )
            if state.revision_count >= self.max_revisions:
                return await self._fail(
                    state,
                    f"Quality Reviewerが{self.max_revisions}回revision_requiredを返したため停止しました",
                    progress_callback,
                )
            target = review.revision_target or ""
            await self._emit(
                progress_callback,
                f"Quality Reviewer: revision_required → {target}（{state.revision_count}/{self.max_revisions}）",
            )
            revision_message = PMPMessage.create(
                workflow_id=state.workflow_id,
                parent_message_id=response.message_id,
                sender_agent_id=self.agent_id,
                receiver_agent_id=target,
                message_type=MessageType.REVISION_REQUEST,
                objective="Revise the rejected Producer artifact",
                payload={
                    "target_agent": target,
                    "reason": review.reason,
                    "required_action": review.required_action,
                },
                context=PMPContext(
                    current_stage="producer.manager",
                    previous_stage="producer.quality_reviewer",
                    next_stage=target,
                ),
                routing=PMPRouting(revision_target=target, reply_required=True),
                metadata=PMPMetadata(
                    status=MessageStatus.REVISION_REQUIRED,
                    extensions={"role_definition": state.role_definition_usage[-1]},
                ),
            )
            state.message_history.append(revision_message)
            state.status = WorkflowStatus.REVISING
            self._invalidate_from(state, target)
            self.repository.save(state)
            return await self._run_from(state, AGENT_ORDER.index(target), progress_callback)
        return await self._fail(state, f"Quality Reviewer blocked workflow: {review.reason}", progress_callback)

    def _invalidate_from(self, state: ProducerWorkflowState, target: str) -> None:
        index = AGENT_ORDER.index(target)
        invalidated = set(AGENT_ORDER[index:])
        state.completed_agents = [agent for agent in state.completed_agents if agent not in invalidated]
        if index <= 0:
            state.topic_candidates = []
        if index <= 1:
            state.selected_topic = None
        if index <= 2:
            state.general_opinion = None
        if index <= 3:
            state.research_plan = None
        state.review_result = None

    def _send_to_researcher(self, state: ProducerWorkflowState) -> None:
        if state.research_plan is None:
            raise ValueError("research_plan is missing")
        self._validate_handoff_payload(state.research_plan)
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="researcher.manager",
            message_type=MessageType.RESEARCH_PLAN,
            objective="Begin evidence collection from the approved Producer research plan",
            payload=state.research_plan,
            context=PMPContext(
                current_stage="producer",
                previous_stage="producer.quality_reviewer",
                next_stage="researcher",
            ),
            metadata=PMPMetadata(
                status=MessageStatus.COMPLETED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        self.pmp_validator.validate(message)
        self.repository.save_researcher_outbox(message)
        state.message_history.append(message)

    @staticmethod
    def _validate_handoff_payload(payload: dict[str, Any]) -> None:
        required = {
            "research_plan_id",
            "topic",
            "general_opinion",
            "research_questions",
            "scope",
            "constraints",
            "topic_id",
            "general_opinion_id",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(f"Producer→Researcher handoff is missing: {', '.join(missing)}")
        for index, question in enumerate(payload["research_questions"]):
            if not question.get("research_question_id"):
                raise ValueError(f"research_questions[{index}] is missing research_question_id")

    @staticmethod
    def _mark_complete(state: ProducerWorkflowState, agent_id: str) -> None:
        if agent_id not in state.completed_agents:
            state.completed_agents.append(agent_id)

    async def _fail(
        self,
        state: ProducerWorkflowState,
        message: str,
        progress_callback: ProgressCallback | None,
    ) -> ProducerWorkflowState:
        state.status = WorkflowStatus.FAILED
        state.error = {"message": message}
        state.current_agent_id = None
        state.completed_at = utc_now()
        self.repository.save(state)
        await self._emit(progress_callback, f"Workflow失敗: {message}")
        return state

    @staticmethod
    async def _emit(callback: ProgressCallback | None, message: str) -> None:
        if callback is None:
            return
        result = callback(message)
        if inspect.isawaitable(result):
            await result
