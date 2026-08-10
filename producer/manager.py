from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
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
from common.validation import PMPValidator
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from producer.registry import ProducerRegistry
from producer.schemas.review import QualityReviewOutput
from producer.state import ProducerWorkflowState, RevisionRecord, utc_now
from producer.workflow import AGENT_ORDER, DISPLAY_NAMES
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
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.max_revisions = max_revisions
        self.pmp_validator = PMPValidator()
        self.rd_loader = rd_loader or registry.rd_loader

    async def start(
        self,
        *,
        user_topic: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ProducerWorkflowState:
        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime_config = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if runtime_config.revision_limit is not None:
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

            request = self._create_task_message(state, agent_id, index)
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
        return await self._fail(state, "Quality Reviewerを通過せずに処理が終了しました", progress_callback)

    def _create_task_message(self, state: ProducerWorkflowState, agent_id: str, index: int) -> PMPMessage:
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
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )

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
