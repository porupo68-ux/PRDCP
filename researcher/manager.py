from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import TypeAdapter

from common.ids import new_id
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
from producer.schemas.research_plan import ResearchPlan, ResearchTarget
from researcher.registry import ResearcherRegistry
from researcher.schemas.research_report import (
    CrossSourceObservation,
    EvidenceGap,
    EvidenceItem,
    EvidenceQualityAssessment,
    ObservationType,
    ResearchQuestionCoverage,
    ResearchReport,
    ResearchReportReview,
)
from researcher.schemas.research_result import CoverageStatus, ResearchResult
from researcher.schemas.research_task import RESEARCH_TARGET_MAP, ResearchTask
from researcher.schemas.external_revision import (
    ExternalResearchRevisionPayload,
    ExternalResearchRevisionRequest,
    external_revision_context,
)
from researcher.schemas.review import ResearchQualityReviewOutput
from researcher.schemas.source import (
    SOURCE_METADATA_MODELS,
    ResearchSource,
    ResearchSourceType,
)
from researcher.state import (
    ExternalResearchRevisionRecord,
    ExternalRevisionCheckpoint,
    ResearchRevisionRecord,
    ResearcherWorkflowState,
    utc_now,
)
from researcher.workflow import DISPLAY_NAMES, QUALITY_REVIEWER_ID
from storage.researcher_workflow_repository import ResearcherWorkflowRepository


ProgressCallback = Callable[[str], Awaitable[None] | None]


class ResearcherManager:
    agent_id = "researcher.manager"

    def __init__(
        self,
        registry: ResearcherRegistry,
        repository: ResearcherWorkflowRepository,
        *,
        max_revisions: int = 3,
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.demo_safe_mode = demo_safe_mode
        self.max_revisions = 0 if demo_safe_mode else max_revisions
        self.pmp_validator = PMPValidator()
        self.rd_loader = rd_loader or registry.rd_loader

    async def start(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        try:
            existing = self.repository.load(workflow_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            return existing
        handoff = self.repository.load_producer_handoff(workflow_id)
        return await self.start_from_message(handoff, progress_callback=progress_callback)

    async def resume(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        state = self.repository.load(workflow_id)
        if state.research_report is None:
            raise ValueError("Researcher workflow has no saved Research Report")
        if state.external_revision_reply_sent:
            raise ValueError("Researcher external revision reply has already been sent")

        request = self.repository.load_revision_request(workflow_id)
        payload = self._validate_external_revision_request(state, request)
        request_ids = [item.revision_request_id for item in payload.revision_requests]
        processed_ids = {
            request_id
            for record in state.external_revision_history
            if record.status in {"completed", "reply_sent"}
            for request_id in record.revision_request_ids
        }
        duplicate_processed = sorted(set(request_ids) & processed_ids)
        if duplicate_processed:
            raise ValueError(
                "External revision request has already been processed: "
                + ", ".join(duplicate_processed)
            )

        manager_snapshot = self.rd_loader.load(self.agent_id)
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())

        active_record = next(
            (
                record
                for record in reversed(state.external_revision_history)
                if record.parent_message_id == request.message_id
                and set(record.revision_request_ids) == set(request_ids)
                and record.status in {"processing", "failed", "blocked"}
            ),
            None,
        )
        if active_record is None:
            revision_tasks = self._build_external_revision_tasks(state, payload)
            state.external_revision_count += 1
            state.pending_external_revision_request_ids = request_ids
            state.pending_revision_parent_message_id = request.message_id
            state.pending_revision_source_agent_id = request.sender_agent_id
            active_record = ExternalResearchRevisionRecord(
                iteration=state.external_revision_count,
                source_agent_id=request.sender_agent_id,
                parent_message_id=request.message_id,
                revision_request_ids=request_ids,
                target_agent_ids=list(
                    dict.fromkeys(task.target_agent_id for task in revision_tasks)
                ),
                status="processing",
            )
            state.external_revision_history.append(active_record)
            state.research_tasks.extend(
                task.model_dump(mode="json") for task in revision_tasks
            )
            if not any(
                message.message_id == request.message_id
                for message in state.message_history
            ):
                state.message_history.append(request)
            state.external_revision_status = ExternalRevisionCheckpoint.REQUEST_RECEIVED
        else:
            revision_tasks = self._saved_external_revision_tasks(state, request_ids)
            if not revision_tasks:
                raise ValueError(
                    "External revision history exists but its saved Research Tasks are missing"
                )
            active_record.status = "processing"
        state.error = None
        state.completed_at = None
        self.repository.save(state)
        await self._emit(
            progress_callback,
            "Deliberation追加調査要求を受領: "
            + ", ".join(active_record.target_agent_ids),
        )

        completed_task_ids = self._completed_research_task_ids(state)
        incomplete_tasks = [
            task for task in revision_tasks if task.task_id not in completed_task_ids
        ]
        already_completed = len(revision_tasks) - len(incomplete_tasks)
        if already_completed:
            await self._emit(
                progress_callback,
                "Existing external revision results detected",
            )
        if incomplete_tasks:
            state.external_revision_status = ExternalRevisionCheckpoint.RESEARCH_DISPATCHED
            self.repository.save(state)
            newly_completed = await self._execute_tasks(
                state,
                incomplete_tasks,
                is_revision=True,
                progress_callback=progress_callback,
            )
        else:
            newly_completed = 0
            await self._emit(
                progress_callback,
                f"Provider dispatch skipped: {already_completed}/{len(revision_tasks)} completed",
            )
        total_completed = already_completed + newly_completed
        if total_completed == 0:
            state.external_revision_history[-1].status = "failed"
            self.repository.save(state)
            return await self._fail(
                state,
                "Deliberationが指定したResearcher Agentがすべて失敗しました",
                progress_callback,
            )
        if total_completed < len(revision_tasks):
            state.external_revision_history[-1].status = "failed"
            self.repository.save(state)
            return await self._fail(
                state,
                "Some external revision Researcher tasks remain incomplete",
                progress_callback,
            )
        state.external_revision_status = (
            ExternalRevisionCheckpoint.RESEARCH_RESULTS_COLLECTED
        )
        self.repository.save(state)
        return await self._integrate_and_review(
            state,
            progress_callback,
            external_request=request,
        )

    @staticmethod
    def _saved_external_revision_tasks(
        state: ResearcherWorkflowState,
        request_ids: list[str],
    ) -> list[ResearchTask]:
        expected_ids = set(request_ids)
        return [
            task
            for raw_task in state.research_tasks
            if (
                context := raw_task.get("revision_context") or {}
            ).get("revision_request_id") in expected_ids
            for task in [ResearchTask.model_validate(raw_task)]
        ]

    @staticmethod
    def _completed_research_task_ids(state: ResearcherWorkflowState) -> set[str]:
        return {
            ResearchResult.model_validate(raw_result).task_id
            for results in state.agent_results.values()
            for raw_result in results
        }

    async def run_task(
        self,
        workflow_id: str,
        task_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        state = self.repository.load(workflow_id)
        task_data = next(
            (item for item in state.research_tasks if item.get("task_id") == task_id),
            None,
        )
        if task_data is None:
            available = ", ".join(
                item.get("task_id", "<missing>") for item in state.research_tasks
            )
            raise ValueError(
                f"Researcher task not found in workflow {workflow_id}: {task_id}. "
                f"Available task IDs: {available or '<none>'}"
            )

        task = ResearchTask.model_validate(task_data)
        state.error = None
        state.completed_at = None
        await self._execute_tasks(
            state,
            [task],
            is_revision=False,
            progress_callback=progress_callback,
        )
        return state

    async def start_from_message(
        self,
        handoff: PMPMessage,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime_config = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime_config.revision_limit is not None:
            self.max_revisions = runtime_config.revision_limit
        plan = self._validate_producer_handoff(handoff)
        tasks = self._create_research_tasks(plan)
        state = ResearcherWorkflowState(
            workflow_id=handoff.workflow_id,
            producer_handoff=handoff.model_dump(mode="json"),
            research_plan=plan.model_dump(mode="json"),
            research_tasks=[task.model_dump(mode="json") for task in tasks],
            message_history=[handoff],
            role_definition_usage=[manager_snapshot.trace()],
        )
        self.repository.save(state)
        await self._emit(
            progress_callback,
            f"Researcher Workflow開始: {state.workflow_id}（{len(tasks)} tasks）",
        )
        completed = await self._execute_tasks(
            state,
            tasks,
            is_revision=False,
            progress_callback=progress_callback,
        )
        if completed == 0:
            return await self._fail(
                state,
                "すべてのResearcher Taskが失敗したため統合できません",
                progress_callback,
            )
        return await self._integrate_and_review(state, progress_callback)

    def _validate_producer_handoff(self, handoff: PMPMessage) -> ResearchPlan:
        self.pmp_validator.validate(handoff)
        if handoff.sender_agent_id != "producer.manager":
            raise ValueError("Producer handoff sender must be producer.manager")
        if handoff.receiver_agent_id != self.agent_id:
            raise ValueError("Producer handoff receiver must be researcher.manager")
        if handoff.message_type != MessageType.RESEARCH_PLAN.value:
            raise ValueError("Producer handoff message_type must be research_plan")
        return ResearchPlan.model_validate(handoff.payload)

    def _validate_external_revision_request(
        self,
        state: ResearcherWorkflowState,
        request: PMPMessage,
    ) -> ExternalResearchRevisionPayload:
        self.pmp_validator.validate(request)
        if request.workflow_id != state.workflow_id:
            raise ValueError("External revision request workflow ID mismatch")
        if request.sender_agent_id != "deliberation.manager":
            raise ValueError("External revision request sender must be deliberation.manager")
        if request.receiver_agent_id != self.agent_id:
            raise ValueError("External revision request receiver must be researcher.manager")
        if request.message_type != MessageType.RESEARCH_REVISION_REQUEST.value:
            raise ValueError("External revision request has an invalid message_type")
        if request.constraints.get("preserve_research_plan_scope") is not True:
            raise ValueError("External revision request must preserve Research Plan scope")
        payload = ExternalResearchRevisionPayload.model_validate(request.payload)
        current_report_id = str(state.research_report.get("research_report_id"))
        if payload.research_report_id != current_report_id:
            raise ValueError("External revision request Research Report ID mismatch")
        request_ids = [item.revision_request_id for item in payload.revision_requests]
        if len(set(request_ids)) != len(request_ids):
            raise ValueError("External revision request IDs must be unique")
        plan = ResearchPlan.model_validate(state.research_plan)
        question_ids = {item.research_question_id for item in plan.research_questions}
        unknown = sorted(
            {
                item.research_question_id
                for item in payload.revision_requests
                if item.research_question_id not in question_ids
            }
        )
        if unknown:
            raise ValueError(
                "External revision references Research Questions outside the Research Plan: "
                + ", ".join(unknown)
            )
        return payload

    @staticmethod
    def _build_external_revision_tasks(
        state: ResearcherWorkflowState,
        payload: ExternalResearchRevisionPayload,
    ) -> list[ResearchTask]:
        plan = ResearchPlan.model_validate(state.research_plan)
        questions = {item.research_question_id: item for item in plan.research_questions}
        tasks: list[ResearchTask] = []
        iteration = state.external_revision_count + 1
        for revision_request in payload.revision_requests:
            question = questions[revision_request.research_question_id]
            allowed = {ResearchTarget(item) for item in question.research_targets}
            selected = list(
                dict.fromkeys(
                    ResearchTarget(item)
                    for item in revision_request.preferred_source_categories
                    if ResearchTarget(item) in allowed
                )
            )
            if not selected:
                raise ValueError(
                    "External revision preferred source categories are outside the approved "
                    f"Research Plan for {revision_request.research_question_id}"
                )
            for target in selected:
                tasks.append(
                    ResearchTask(
                        task_id=new_id("task"),
                        research_question_id=question.research_question_id,
                        target_agent_id=RESEARCH_TARGET_MAP[target],
                        research_target=target,
                        question=question.question,
                        scope=plan.scope,
                        constraints=plan.constraints,
                        max_sources=5,
                        revision_context=external_revision_context(
                            revision_request,
                            iteration=iteration,
                        ),
                    )
                )
        if not tasks:
            raise ValueError("External revision request did not produce any Researcher tasks")
        return tasks

    @staticmethod
    def _create_research_tasks(plan: ResearchPlan) -> list[ResearchTask]:
        tasks: list[ResearchTask] = []
        for question in plan.research_questions:
            for raw_target in question.research_targets:
                target = ResearchTarget(raw_target)
                try:
                    agent_id = RESEARCH_TARGET_MAP[target]
                except KeyError as exc:
                    raise ValueError(f"Unknown research target: {target}") from exc
                tasks.append(
                    ResearchTask(
                        task_id=new_id("task"),
                        research_question_id=question.research_question_id,
                        target_agent_id=agent_id,
                        research_target=target,
                        question=question.question,
                        scope=plan.scope,
                        constraints=plan.constraints,
                        max_sources=5,
                    )
                )
        if not tasks:
            raise ValueError("Research Plan did not produce any Researcher tasks")
        return tasks

    async def _execute_tasks(
        self,
        state: ResearcherWorkflowState,
        tasks: list[ResearchTask],
        *,
        is_revision: bool,
        progress_callback: ProgressCallback | None,
    ) -> int:
        state.status = WorkflowStatus.DISPATCHING
        state.current_agent_ids = list(dict.fromkeys(task.target_agent_id for task in tasks))
        requests = [self._create_task_message(state, task, is_revision=is_revision) for task in tasks]
        state.message_history.extend(requests)
        self.repository.save(state)

        state.status = WorkflowStatus.COLLECTING
        self.repository.save(state)
        responses = await asyncio.gather(
            *[
                self.registry.get(task.target_agent_id).execute(request)
                for task, request in zip(tasks, requests, strict=True)
            ],
            return_exceptions=True,
        )

        completed = 0
        for index, (task, request, response) in enumerate(
            zip(tasks, requests, responses, strict=True),
            start=1,
        ):
            task_succeeded = False
            if isinstance(response, BaseException):
                self._record_failure(state, task, f"Unhandled agent exception: {response}")
            else:
                state.message_history.append(response)
                error = self._validate_specialist_response(
                    task,
                    request,
                    response,
                    is_revision=is_revision,
                )
                if error:
                    self._record_failure(state, task, error)
                else:
                    result = ResearchResult.model_validate(response.payload)
                    state.agent_results.setdefault(task.target_agent_id, []).append(
                        result.model_dump(mode="json")
                    )
                    if task.target_agent_id not in state.completed_agents:
                        state.completed_agents.append(task.target_agent_id)
                    if task.target_agent_id in state.failed_agents:
                        state.failed_agents.remove(task.target_agent_id)
                    completed += 1
                    task_succeeded = True
            self.repository.save(state)
            await self._emit(
                progress_callback,
                f"[{index}/{len(tasks)}] {DISPLAY_NAMES[task.target_agent_id]} "
                + ("完了" if task_succeeded else "失敗（limitationとして記録）"),
            )

        state.current_agent_ids = []
        state.status = (
            WorkflowStatus.PARTIALLY_COMPLETED if completed < len(tasks) else WorkflowStatus.RUNNING
        )
        self.repository.save(state)
        return completed

    def _create_task_message(
        self,
        state: ResearcherWorkflowState,
        task: ResearchTask,
        *,
        is_revision: bool,
    ) -> PMPMessage:
        return PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=task.target_agent_id,
            message_type=(
                MessageType.RESEARCH_REVISION_REQUEST if is_revision else MessageType.TASK
            ),
            objective=(
                "Collect additional evidence requested by Researcher Quality Reviewer"
                if is_revision
                else "Collect and organize evidence for an approved Research Question"
            ),
            payload=task.model_dump(mode="json"),
            constraints={
                "do_not_draw_conclusions": True,
                "do_not_change_research_question": True,
                "prefer_primary_sources": True,
            },
            context=PMPContext(
                current_stage=task.target_agent_id,
                previous_stage="researcher.manager",
                next_stage="researcher.manager",
            ),
            routing=PMPRouting(
                revision_target=task.target_agent_id if is_revision else None,
                reply_required=True,
            ),
            metadata=PMPMetadata(
                status=(MessageStatus.REVISION_REQUIRED if is_revision else MessageStatus.QUEUED),
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )

    def _validate_specialist_response(
        self,
        task: ResearchTask,
        request: PMPMessage,
        response: PMPMessage,
        *,
        is_revision: bool,
    ) -> str | None:
        try:
            self.pmp_validator.validate(response)
        except Exception as exc:
            return f"Invalid PMP response from {task.target_agent_id}: {exc}"
        if response.workflow_id != request.workflow_id:
            return f"Workflow ID mismatch from {task.target_agent_id}"
        if response.parent_message_id != request.message_id:
            return f"Parent message ID mismatch from {task.target_agent_id}"
        if response.sender_agent_id != task.target_agent_id or response.receiver_agent_id != self.agent_id:
            return f"Invalid routing in response from {task.target_agent_id}"
        if response.message_type == MessageType.ERROR.value:
            return f"{task.target_agent_id}: {response.payload.get('message', 'unknown error')}"
        expected = (
            MessageType.RESEARCH_REVISION_RESULT.value
            if is_revision
            else MessageType.RESULT.value
        )
        if response.message_type != expected:
            return f"Unexpected message type from {task.target_agent_id}: {response.message_type}"
        try:
            result = ResearchResult.model_validate(response.payload)
        except Exception as exc:
            return f"Invalid Research Result from {task.target_agent_id}: {exc}"
        if result.task_id != task.task_id:
            return f"Task ID mismatch from {task.target_agent_id}"
        if result.research_question_id != task.research_question_id:
            return f"Research Question ID mismatch from {task.target_agent_id}"
        if result.agent_id != task.target_agent_id:
            return f"Agent ID mismatch from {task.target_agent_id}"
        return None

    @staticmethod
    def _record_failure(
        state: ResearcherWorkflowState,
        task: ResearchTask,
        message: str,
    ) -> None:
        if task.target_agent_id not in state.failed_agents:
            state.failed_agents.append(task.target_agent_id)
        state.limitations.append(
            {
                "task_id": task.task_id,
                "research_question_id": task.research_question_id,
                "agent_id": task.target_agent_id,
                "message": message,
            }
        )

    async def _integrate_and_review(
        self,
        state: ResearcherWorkflowState,
        progress_callback: ProgressCallback | None,
        *,
        external_request: PMPMessage | None = None,
    ) -> ResearcherWorkflowState:
        while True:
            state.status = WorkflowStatus.INTEGRATING
            if external_request is not None:
                state.external_revision_status = (
                    ExternalRevisionCheckpoint.REPORT_INTEGRATING
                )
                self.repository.save(state)
            report = self._build_report(state)
            state.collected_sources = [source.model_dump(mode="json") for source in report.sources]
            state.research_report = report.model_dump(mode="json")
            self.repository.save_report(report)
            self.repository.save(state)
            await self._emit(
                progress_callback,
                f"Research Report統合完了: {len(report.sources)} sources",
            )

            try:
                if external_request is not None:
                    state.external_revision_status = (
                        ExternalRevisionCheckpoint.QUALITY_REVIEWING
                    )
                    self.repository.save(state)
                review, review_response = await self._request_review(
                    state,
                    report,
                    external_revision=(
                        state.external_revision_history[-1]
                        if external_request is not None
                        else None
                    ),
                )
            except Exception as exc:
                return await self._fail(
                    state,
                    f"Quality Reviewerの応答を検証できません: {exc}",
                    progress_callback,
                )
            review_summary = review.model_dump(
                mode="json",
                exclude={"approved_research_report"},
            )
            state.review_result = review_summary
            self.repository.save(state)

            if review.status in {"approved", "approved_with_conditions"}:
                approved = review.approved_research_report or report
                approved.review = ResearchReportReview.model_validate(review_summary)
                state.research_report = approved.model_dump(mode="json")
                state.status = WorkflowStatus.APPROVED
                self.repository.save_report(approved)
                self.repository.save(state)
                await self._emit(
                    progress_callback,
                    f"Quality Reviewer: {review.status}",
                )
                try:
                    if external_request is not None:
                        state.external_revision_history[-1].status = "completed"
                        reply = self._send_revision_result_to_deliberation(
                            state,
                            approved,
                            external_request,
                        )
                        state.external_revision_history[-1].status = "reply_sent"
                        state.external_revision_history[-1].completed_at = utc_now()
                        state.external_revision_history[-1].reply_message_id = reply.message_id
                        state.external_revision_reply_sent = True
                        state.external_revision_status = (
                            ExternalRevisionCheckpoint.COMPLETED_REVISION
                        )
                        state.pending_external_revision_request_ids = []
                        state.pending_revision_parent_message_id = None
                        state.pending_revision_source_agent_id = None
                    else:
                        self._send_to_deliberation(
                            state,
                            approved,
                            review_response.message_id,
                        )
                except Exception as exc:
                    if external_request is not None:
                        state.external_revision_history[-1].status = "failed"
                    return await self._fail(
                        state,
                        f"Deliberation Outboxへの送信に失敗しました: {exc}",
                        progress_callback,
                    )
                if external_request is None:
                    state.deliberation_sent = True
                state.status = (
                    WorkflowStatus.COMPLETED_REVISION
                    if external_request is not None
                    else WorkflowStatus.COMPLETED
                )
                state.current_agent_ids = []
                state.completed_at = utc_now()
                self.repository.save(state)
                return state

            if review.status == "blocked":
                if external_request is not None:
                    state.external_revision_history[-1].status = "blocked"
                return await self._block(state, review.reason, progress_callback)

            if self.demo_safe_mode:
                if external_request is not None:
                    state.external_revision_history[-1].status = "blocked"
                return await self._block(
                    state,
                    "Demo Safe Mode stopped automatic reviewer revision and Manager re-dispatch",
                    progress_callback,
                )

            state.revision_count += 1
            state.revision_history.append(
                ResearchRevisionRecord(
                    iteration=state.revision_count,
                    target_agent_ids=review.revision_targets,
                    findings=[finding.model_dump(mode="json") for finding in review.findings],
                )
            )
            if state.revision_count >= self.max_revisions:
                return await self._block(
                    state,
                    f"Quality Reviewerが{self.max_revisions}回revision_requiredを返したため停止しました",
                    progress_callback,
                )
            try:
                revision_tasks = self._build_revision_tasks(state, review)
            except ValueError as exc:
                return await self._fail(state, str(exc), progress_callback)
            state.status = WorkflowStatus.REVISING
            self.repository.save(state)
            await self._emit(
                progress_callback,
                "Quality Reviewer: revision_required → "
                + ", ".join(review.revision_targets)
                + f"（{state.revision_count}/{self.max_revisions}）",
            )
            completed = await self._execute_tasks(
                state,
                revision_tasks,
                is_revision=True,
                progress_callback=progress_callback,
            )
            if completed == 0:
                return await self._fail(
                    state,
                    "修正対象Researcherがすべて失敗しました",
                    progress_callback,
                )

    async def _request_review(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        *,
        external_revision: ExternalResearchRevisionRecord | None = None,
    ) -> tuple[ResearchQualityReviewOutput, PMPMessage]:
        state.status = WorkflowStatus.REVIEWING
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=QUALITY_REVIEWER_ID,
            message_type=MessageType.TASK,
            objective="Review Research Report against the approved Research Plan",
            payload={
                "research_plan": state.research_plan,
                "research_report": report.model_dump(mode="json"),
                "revision_context": (
                    external_revision.model_dump(mode="json")
                    if external_revision is not None
                    else (
                        state.revision_history[-1].model_dump(mode="json")
                        if state.revision_history
                        else None
                    )
                ),
            },
            constraints={
                "do_not_research": True,
                "do_not_draw_conclusions": True,
                "only_require_plan_scope": True,
            },
            context=PMPContext(
                current_stage=QUALITY_REVIEWER_ID,
                previous_stage="researcher.manager",
                next_stage="researcher.manager",
            ),
            metadata=PMPMetadata(
                status=MessageStatus.QUEUED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        state.message_history.append(request)
        self.repository.save(state)
        response = await self.registry.get(QUALITY_REVIEWER_ID).execute(request)
        state.message_history.append(response)
        self.repository.save(state)
        error = self._validate_review_response(request, response)
        if error:
            raise ValueError(error)
        return ResearchQualityReviewOutput.model_validate(response.payload), response

    def _validate_review_response(self, request: PMPMessage, response: PMPMessage) -> str | None:
        try:
            self.pmp_validator.validate(response)
        except Exception as exc:
            return f"Invalid Quality Reviewer PMP: {exc}"
        if response.workflow_id != request.workflow_id:
            return "Quality Reviewer workflow ID mismatch"
        if response.parent_message_id != request.message_id:
            return "Quality Reviewer parent message ID mismatch"
        if response.sender_agent_id != QUALITY_REVIEWER_ID or response.receiver_agent_id != self.agent_id:
            return "Invalid Quality Reviewer routing"
        if response.message_type == MessageType.ERROR.value:
            return f"Quality Reviewer error: {response.payload.get('message', 'unknown error')}"
        if response.message_type != MessageType.REVIEW.value:
            return f"Unexpected Quality Reviewer message type: {response.message_type}"
        return None

    @staticmethod
    def _build_revision_tasks(
        state: ResearcherWorkflowState,
        review: ResearchQualityReviewOutput,
    ) -> list[ResearchTask]:
        originals = [ResearchTask.model_validate(item) for item in state.research_tasks]
        selected: dict[tuple[str, str], ResearchTask] = {}
        for finding in review.findings:
            if finding.target_agent_id is None:
                continue
            matches = [
                task
                for task in originals
                if task.target_agent_id == finding.target_agent_id
                and (
                    finding.research_question_id is None
                    or task.research_question_id == finding.research_question_id
                )
            ]
            if not matches:
                raise ValueError(
                    "Quality Reviewer requested a Researcher outside the approved Research Plan: "
                    f"{finding.target_agent_id}"
                )
            for original in matches:
                data = original.model_dump(mode="json")
                data["task_id"] = new_id("task")
                data["revision_context"] = {
                    "finding_id": finding.finding_id,
                    "issue": finding.issue,
                    "required_action": finding.required_action,
                    "revision_iteration": state.revision_count,
                }
                selected[(original.target_agent_id, original.research_question_id)] = (
                    ResearchTask.model_validate(data)
                )
        for target in review.revision_targets:
            if any(key[0] == target for key in selected):
                continue
            matches = [task for task in originals if task.target_agent_id == target]
            if not matches:
                raise ValueError(
                    f"Quality Reviewer requested an out-of-plan revision target: {target}"
                )
            for original in matches:
                data = original.model_dump(mode="json")
                data["task_id"] = new_id("task")
                data["revision_context"] = {
                    "issue": review.reason,
                    "required_action": "Resolve Quality Reviewer findings",
                    "revision_iteration": state.revision_count,
                }
                selected[(original.target_agent_id, original.research_question_id)] = (
                    ResearchTask.model_validate(data)
                )
        if not selected:
            raise ValueError("revision_required review did not resolve to any Research Task")
        return list(selected.values())

    def _build_report(self, state: ResearcherWorkflowState) -> ResearchReport:
        plan = ResearchPlan.model_validate(state.research_plan)
        raw_sources: list[ResearchSource] = []
        result_limitations: list[str] = []
        for agent_id, raw_results in state.agent_results.items():
            for raw_result in raw_results:
                result = ResearchResult.model_validate(raw_result)
                raw_sources.extend(result.sources)
                result_limitations.extend(
                    f"{agent_id}: {limitation}" for limitation in result.limitations
                )
        sources = self._deduplicate_sources(raw_sources)
        self._validate_source_metadata_contracts(sources)
        question_coverages: list[ResearchQuestionCoverage] = []
        gaps: list[EvidenceGap] = []
        unresolved: list[str] = []

        for question in plan.research_questions:
            required = [ResearchTarget(item).value for item in question.research_targets]
            related = [
                source
                for source in sources
                if question.research_question_id in source.research_question_ids
            ]
            completed = list(
                dict.fromkeys(str(source.source_type) for source in related)
            )
            missing = [category for category in required if category not in completed]
            coverage_status = (
                CoverageStatus.COMPLETE
                if not missing
                else CoverageStatus.PARTIAL
                if completed
                else CoverageStatus.NO_RESULT
            )
            question_coverages.append(
                ResearchQuestionCoverage(
                    research_question_id=question.research_question_id,
                    question=question.question,
                    required_categories=required,
                    completed_categories=completed,
                    evidence_ids=[source.evidence_id for source in related],
                    coverage_status=coverage_status,
                )
            )
            for category in missing:
                gaps.append(
                    EvidenceGap(
                        gap_id=new_id("gap"),
                        research_question_id=question.research_question_id,
                        missing_category=category,
                        description=f"{category} evidence is missing for this Research Question",
                    )
                )
            if missing:
                unresolved.append(
                    f"{question.research_question_id}: missing {', '.join(missing)} evidence"
                )

        sources_by_category: dict[str, list[str]] = {}
        for source in sources:
            sources_by_category.setdefault(str(source.source_type), []).append(source.source_id)

        limitations = list(
            dict.fromkeys(
                [item.get("message", str(item)) for item in state.limitations]
                + result_limitations
                + [
                    limitation
                    for source in sources
                    for limitation in source.limitations
                ]
            )
        )
        evidence_items = [
            EvidenceItem(
                evidence_id=source.evidence_id,
                source_id=source.source_id,
                research_question_ids=source.research_question_ids,
                summary=source.summary,
                stance=source.stance,
                directness=source.directness,
            )
            for source in sources
        ]
        assessments = [
            EvidenceQualityAssessment(
                evidence_id=source.evidence_id,
                source_id=source.source_id,
                reliability=source.reliability,
                directness=source.directness,
                primary_source=source.primary_source,
                limitations=source.limitations,
            )
            for source in sources
        ]
        source_metadata = [
            {
                "source_id": source.source_id,
                "source_type": source.source_type,
                "title": source.title,
                "source_name": source.source_name,
                "url": str(source.url),
                "author_or_organization": source.author_or_organization,
                "published_at": source.published_at.isoformat() if source.published_at else None,
                "retrieved_at": source.retrieved_at.isoformat(),
                "geographic_scope": source.geographic_scope,
                "time_scope": source.time_scope,
                "source_specific_metadata": source.source_specific_metadata,
            }
            for source in sources
        ]
        report_id = (
            state.research_report.get("research_report_id")
            if state.research_report
            else new_id("report")
        )
        return ResearchReport(
            research_report_id=report_id,
            workflow_id=state.workflow_id,
            research_plan_id=plan.research_plan_id,
            topic=plan.topic,
            general_opinion=plan.general_opinion,
            research_questions=question_coverages,
            research_scope=plan.scope,
            sources=sources,
            evidence_items=evidence_items,
            source_metadata=source_metadata,
            source_perspectives=sources_by_category,
            evidence_quality_assessments=assessments,
            research_limitations=limitations,
            unresolved_questions=unresolved,
            sources_by_category=sources_by_category,
            source_count_by_category={
                category: len(source_ids)
                for category, source_ids in sources_by_category.items()
            },
            cross_source_observations=self._build_cross_source_observations(
                question_coverages,
                sources,
            ),
            evidence_gaps=gaps,
            review=None,
        )

    @staticmethod
    def _validate_source_metadata_contracts(sources: list[ResearchSource]) -> None:
        for source in sources:
            source_type = ResearchSourceType(source.source_type)
            metadata_model = SOURCE_METADATA_MODELS[source_type]
            try:
                TypeAdapter(metadata_model).validate_python(
                    source.source_specific_metadata
                )
            except Exception as exc:
                raise ValueError(
                    f"{source_type.value} source metadata does not match its schema: {exc}"
                ) from exc

    @staticmethod
    def _build_cross_source_observations(
        coverages: list[ResearchQuestionCoverage],
        sources: list[ResearchSource],
    ) -> list[CrossSourceObservation]:
        observations: list[CrossSourceObservation] = []
        for coverage in coverages:
            related = [
                source
                for source in sources
                if coverage.research_question_id in source.research_question_ids
            ]
            stances = {str(source.stance) for source in related}
            if "SUPPORTS" in stances and "OPPOSES" in stances:
                observations.append(
                    CrossSourceObservation(
                        observation_id=new_id("obs"),
                        description=(
                            "Collected sources include both supporting and opposing evidence; "
                            "no truth judgment has been made by Researcher."
                        ),
                        supporting_evidence_ids=[source.evidence_id for source in related],
                        observation_type=ObservationType.DISAGREEMENT,
                        limitations=["Interpretation is reserved for Deliberation"],
                    )
                )
        return observations

    def _deduplicate_sources(self, sources: list[ResearchSource]) -> list[ResearchSource]:
        unique: list[ResearchSource] = []
        for source in sources:
            duplicate = next(
                (candidate for candidate in unique if self._same_source(candidate, source)),
                None,
            )
            if duplicate is None:
                unique.append(source.model_copy(deep=True))
                continue
            duplicate.research_question_ids = list(
                dict.fromkeys(duplicate.research_question_ids + source.research_question_ids)
            )
            duplicate.geographic_scope = list(
                dict.fromkeys(duplicate.geographic_scope + source.geographic_scope)
            )
            duplicate.limitations = list(
                dict.fromkeys(duplicate.limitations + source.limitations)
            )
            if source.evidence_id != duplicate.evidence_id:
                merged_ids = duplicate.source_specific_metadata.setdefault(
                    "merged_evidence_ids",
                    [],
                )
                if source.evidence_id not in merged_ids:
                    merged_ids.append(source.evidence_id)
        return unique

    @classmethod
    def _same_source(cls, left: ResearchSource, right: ResearchSource) -> bool:
        left_doi = str(left.source_specific_metadata.get("doi") or "").strip().lower()
        right_doi = str(right.source_specific_metadata.get("doi") or "").strip().lower()
        if left_doi and right_doi and left_doi == right_doi:
            return True
        if cls._normalise_url(str(left.url)) == cls._normalise_url(str(right.url)):
            return True
        left_date = left.published_at.date().isoformat() if left.published_at else ""
        right_date = right.published_at.date().isoformat() if right.published_at else ""
        left_org = (left.author_or_organization or left.source_name).strip().lower()
        right_org = (right.author_or_organization or right.source_name).strip().lower()
        left_title = " ".join(left.title.lower().split())
        right_title = " ".join(right.title.lower().split())
        if left_title == right_title and left_org == right_org and left_date == right_date:
            return True
        return (
            left_org == right_org
            and SequenceMatcher(None, left_title, right_title).ratio() >= 0.93
        )

    @staticmethod
    def _normalise_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                parts.path.rstrip("/") or "/",
                "",
                "",
            )
        )

    def _send_to_deliberation(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        parent_message_id: str,
    ) -> None:
        report_payload = report.model_dump(mode="json")
        payload = {
            **report_payload,
            "research_report": report_payload,
            "quality_review": state.review_result or {},
            "known_limitations": report.research_limitations,
            "unresolved_gaps": [gap.model_dump(mode="json") for gap in report.evidence_gaps],
        }
        self._validate_deliberation_handoff(payload)
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=parent_message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="deliberation.manager",
            message_type=MessageType.RESEARCH_RESULT,
            objective="Provide validated Research Report to Deliberation",
            payload=payload,
            context=PMPContext(
                current_stage="researcher",
                previous_stage="researcher.quality_reviewer",
                next_stage="deliberation",
            ),
            metadata=PMPMetadata(
                status=MessageStatus.COMPLETED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        self.pmp_validator.validate(message)
        self.repository.save_deliberation_outbox(message)
        state.message_history.append(message)

    def _send_revision_result_to_deliberation(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        original_request: PMPMessage,
    ) -> PMPMessage:
        report_payload = report.model_dump(mode="json")
        payload = {
            **report_payload,
            "research_report": report_payload,
            "resolved_revision_request_ids": list(
                state.pending_external_revision_request_ids
            ),
            "quality_review": state.review_result or {},
            "known_limitations": report.research_limitations,
            "unresolved_gaps": [
                gap.model_dump(mode="json") for gap in report.evidence_gaps
            ],
        }
        self._validate_deliberation_handoff(payload)
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=original_request.message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="deliberation.manager",
            message_type=MessageType.RESEARCH_REVISION_RESULT,
            objective="Return updated Research Report for Deliberation revision",
            payload=payload,
            context=PMPContext(
                current_stage="researcher.external_revision",
                previous_stage="researcher.quality_reviewer",
                next_stage="deliberation.resume",
            ),
            metadata=PMPMetadata(
                status=MessageStatus.COMPLETED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        self.pmp_validator.validate(message)
        self.repository.save_deliberation_outbox(message)
        state.message_history.append(message)
        return message

    @staticmethod
    def _validate_deliberation_handoff(payload: dict[str, Any]) -> None:
        required = {
            "research_report_id",
            "research_plan_id",
            "topic",
            "general_opinion",
            "research_questions",
            "research_scope",
            "evidence_items",
            "source_metadata",
            "source_perspectives",
            "evidence_quality_assessments",
            "research_limitations",
            "unresolved_questions",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(
                f"Researcher→Deliberation handoff is missing: {', '.join(missing)}"
            )
        for index, question in enumerate(payload["research_questions"]):
            if not question.get("research_question_id"):
                raise ValueError(
                    f"research_questions[{index}] is missing research_question_id"
                )
        for index, evidence in enumerate(payload["evidence_items"]):
            if not evidence.get("evidence_id") or not evidence.get("source_id"):
                raise ValueError(
                    f"evidence_items[{index}] is missing evidence_id or source_id"
                )

    async def _block(
        self,
        state: ResearcherWorkflowState,
        message: str,
        progress_callback: ProgressCallback | None,
    ) -> ResearcherWorkflowState:
        state.status = WorkflowStatus.BLOCKED
        state.error = {"message": message}
        state.current_agent_ids = []
        state.completed_at = utc_now()
        self.repository.save(state)
        await self._emit(progress_callback, f"Researcher Workflow停止: {message}")
        return state

    async def _fail(
        self,
        state: ResearcherWorkflowState,
        message: str,
        progress_callback: ProgressCallback | None,
    ) -> ResearcherWorkflowState:
        state.status = WorkflowStatus.FAILED
        state.error = {"message": message}
        state.current_agent_ids = []
        state.completed_at = utc_now()
        self.repository.save(state)
        await self._emit(progress_callback, f"Researcher Workflow失敗: {message}")
        return state

    @staticmethod
    async def _emit(callback: ProgressCallback | None, message: str) -> None:
        if callback is None:
            return
        result = callback(message)
        if inspect.isawaitable(result):
            await result
