from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

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
from deliberation.registry import DeliberationRegistry
from deliberation.schemas.analysis_task import (
    ANALYSIS_AGENT_MAP,
    AnalysisType,
    CounterargumentTask,
    DeliberationAnalysisTask,
)
from deliberation.schemas.argument_analysis import ArgumentAnalysisResult
from deliberation.schemas.causal_structural_analysis import CausalStructuralAnalysisResult
from deliberation.schemas.counterargument_analysis import CounterargumentAnalysisResult
from deliberation.schemas.deliberation_result import DeliberationResult
from deliberation.schemas.integrated_analysis import FinalIntegratedAnalysis, InitialIntegratedAnalysis
from deliberation.schemas.review import (
    DeliberationQualityReviewInput,
    DeliberationQualityReviewOutput,
    DeterministicValidationResult,
    QualityGateDecision,
)
from deliberation.schemas.stakeholder_response_analysis import StakeholderResponseAnalysisResult
from deliberation.state import (
    DeliberationRevisionRecord,
    DeliberationWorkflowState,
    UpstreamRevisionRecord,
    utc_now,
)
from deliberation.validator import DeliberationValidator
from deliberation.workflow import (
    COUNTERARGUMENT_ANALYST_ID,
    DISPLAY_NAMES,
    PRIMARY_ANALYST_IDS,
    QUALITY_REVIEWER_ID,
)
from researcher.schemas.research_report import ResearchReport
from storage.deliberation_workflow_repository import DeliberationWorkflowRepository


ProgressCallback = Callable[[str], Awaitable[None] | None]


class DeliberationManager:
    agent_id = "deliberation.manager"

    def __init__(
        self,
        registry: DeliberationRegistry,
        repository: DeliberationWorkflowRepository,
        *,
        max_revisions: int = 2,
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.demo_safe_mode = demo_safe_mode
        self.max_revisions = 0 if demo_safe_mode else max_revisions
        self.pmp_validator = PMPValidator()
        self.deterministic_validator = DeliberationValidator()
        self.rd_loader = rd_loader or registry.rd_loader

    async def start(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        try:
            return self.repository.load(workflow_id)
        except FileNotFoundError:
            pass
        handoff = self.repository.load_researcher_handoff(workflow_id)
        return await self.start_from_message(handoff, progress_callback=progress_callback)

    async def start_from_message(
        self,
        handoff: PMPMessage,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime_config = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime_config.revision_limit is not None:
            self.max_revisions = runtime_config.revision_limit
        report = self._validate_researcher_handoff(handoff)
        tasks = self._create_analysis_tasks(report)
        state = DeliberationWorkflowState(
            workflow_id=handoff.workflow_id,
            researcher_handoff=handoff.model_dump(mode="json"),
            research_report=report.model_dump(mode="json"),
            analysis_tasks=[task.model_dump(mode="json") for task in tasks],
            message_history=[handoff],
            role_definition_usage=[manager_snapshot.trace()],
        )
        self.repository.save(state)
        await self._emit(progress_callback, f"Deliberation Workflow開始: {state.workflow_id}")
        completed = await self._execute_primary_tasks(
            state,
            tasks,
            is_revision=False,
            progress_callback=progress_callback,
        )
        if completed == 0:
            return await self._fail(state, "一次分析Agentがすべて失敗しました", progress_callback)
        if completed == 1:
            return await self._block(
                state,
                "一次分析が1系統しか成功せず、多角的統合を実行できません",
                progress_callback,
            )
        if completed == 2:
            state.limitations.append(
                {
                    "stage": "primary_analysis",
                    "message": "一次分析3系統のうち2系統のみで統合を継続しました",
                    "failed_agent_ids": list(state.failed_agents),
                }
            )
            self.repository.save(state)
        return await self._integrate_and_review(
            state,
            rerun_initial=True,
            rerun_counterargument=True,
            progress_callback=progress_callback,
        )

    async def resume(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.WAITING_UPSTREAM_REVISION.value:
            raise ValueError("Deliberation workflow is not waiting for an upstream revision")
        manager_snapshot = self.rd_loader.load(self.agent_id)
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())
        handoff = self.repository.load_researcher_handoff(workflow_id)
        previous_message_id = state.researcher_handoff.get("message_id")
        if handoff.message_id == previous_message_id:
            raise ValueError("Researcherから新しいrevision resultがまだ届いていません")
        report = self._validate_researcher_handoff(handoff, allow_revision=True)
        tasks = self._create_analysis_tasks(report)
        pending_review = self._pending_revision_review(state)
        pending_targets = list(state.pending_revision_targets)
        state.researcher_handoff = handoff.model_dump(mode="json")
        state.research_report = report.model_dump(mode="json")
        state.analysis_tasks = [task.model_dump(mode="json") for task in tasks]
        state.deliberation_result = None
        state.failed_agents = []
        state.current_agent_ids = []
        state.error = None
        state.completed_at = None
        state.status = WorkflowStatus.RUNNING
        state.message_history.append(handoff)
        await self._emit(progress_callback, "Researcher追加調査結果を受領し、Deliberationを再開します")

        if self.demo_safe_mode:
            return await self._block(
                state,
                "Demo Safe Mode stopped pending Deliberation revision after Researcher return",
                progress_callback,
            )

        # Upstream-only revisions still rerun Manager integration so that the
        # updated report reaches downstream checkpoints without rerunning every
        # primary analyst.
        effective_targets = pending_targets or [self.agent_id]
        self._clear_pending_revision(state)
        outcome, rerun_initial, rerun_counterargument = await self._start_internal_revision(
            state,
            pending_review,
            targets=effective_targets,
            progress_callback=progress_callback,
        )
        if outcome is not None:
            return outcome
        return await self._integrate_and_review(
            state,
            rerun_initial=rerun_initial,
            rerun_counterargument=rerun_counterargument,
            progress_callback=progress_callback,
        )

    async def recover(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        state = self.repository.load(workflow_id)
        blocked_review_rerecovery = (
            state.status == WorkflowStatus.BLOCKED.value
            and state.review_result is not None
            and all(
                checkpoint is not None
                for checkpoint in (
                    state.initial_integration,
                    state.counterargument_analysis,
                    state.final_integration,
                    state.deterministic_validation,
                )
            )
        )
        if state.status == WorkflowStatus.COMPLETED.value:
            return state
        if state.status == WorkflowStatus.WAITING_UPSTREAM_REVISION.value:
            raise ValueError(
                "Deliberation workflow is waiting for Researcher evidence; use resume() instead"
            )
        if state.status == WorkflowStatus.BLOCKED.value and not blocked_review_rerecovery:
            raise ValueError(
                "Blocked Deliberation workflows are not eligible for checkpoint recovery"
            )

        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime_config = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime_config.revision_limit is not None:
            self.max_revisions = runtime_config.revision_limit
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())

        try:
            handoff = PMPMessage.model_validate(state.researcher_handoff)
            report = self._validate_researcher_handoff(handoff, allow_revision=True)
            if handoff.workflow_id != state.workflow_id or report.workflow_id != state.workflow_id:
                raise ValueError("Saved Researcher handoff does not match the workflow ID")
            tasks = self._recovery_primary_tasks(state, report)
        except Exception as exc:
            return await self._fail(
                state,
                f"Deliberation recovery could not validate the saved upstream state: {exc}",
                progress_callback,
            )

        state.status = WorkflowStatus.RUNNING
        state.error = None
        state.completed_at = None
        state.current_agent_ids = []
        self.repository.save(state)
        await self._emit(progress_callback, f"Deliberation checkpoint recovery開始: {workflow_id}")

        valid_primary_ids: set[str] = set()
        incomplete_tasks: list[DeliberationAnalysisTask] = []
        downstream_exists = state.initial_integration is not None
        for task in tasks:
            payload = state.analysis_results.get(task.target_agent_id)
            if payload is not None and self._saved_analysis_is_valid(task, payload):
                valid_primary_ids.add(task.target_agent_id)
                continue
            if payload is not None:
                state.analysis_results.pop(task.target_agent_id, None)
                if task.target_agent_id in state.completed_agents:
                    state.completed_agents.remove(task.target_agent_id)
            tolerated_failure = (
                payload is None
                and downstream_exists
                and task.target_agent_id in state.failed_agents
            )
            if not tolerated_failure:
                incomplete_tasks.append(task)

        if len(valid_primary_ids) >= 2 and downstream_exists:
            incomplete_tasks = [
                task
                for task in incomplete_tasks
                if task.target_agent_id not in state.failed_agents
            ]

        if incomplete_tasks:
            recovery_tasks = [self._make_recovery_task(task) for task in incomplete_tasks]
            self._replace_analysis_tasks(state, recovery_tasks)
            self._clear_recovery_checkpoints(state, "initial_integration")
            self.repository.save(state)
            await self._emit(
                progress_callback,
                "未完了一次分析のみ再実行: "
                + ", ".join(task.target_agent_id for task in recovery_tasks),
            )
            await self._execute_primary_tasks(
                state,
                recovery_tasks,
                is_revision=True,
                progress_callback=progress_callback,
            )

        valid_primary_count = sum(
            1
            for task in self._recovery_primary_tasks(state, report)
            if (payload := state.analysis_results.get(task.target_agent_id)) is not None
            and self._saved_analysis_is_valid(task, payload)
        )
        if valid_primary_count == 0:
            return await self._fail(
                state,
                "Checkpoint recovery後も一次分析Agentがすべて未完了です",
                progress_callback,
            )
        if valid_primary_count == 1:
            return await self._block(
                state,
                "Checkpoint recovery後も一次分析が1系統のみのため統合できません",
                progress_callback,
            )

        try:
            if not self._checkpoint_is_current(state, "initial_integration"):
                raise ValueError("initial integration belongs to an earlier revision")
            initial = InitialIntegratedAnalysis.model_validate(state.initial_integration)
        except Exception:
            self._clear_recovery_checkpoints(state, "initial_integration")
            self.repository.save(state)
            return await self._integrate_and_review(
                state,
                rerun_initial=True,
                rerun_counterargument=True,
                progress_callback=progress_callback,
                recovery=True,
            )

        try:
            if not self._checkpoint_is_current(state, "counterargument"):
                raise ValueError("counterargument belongs to an earlier revision")
            CounterargumentAnalysisResult.model_validate(state.counterargument_analysis)
        except Exception:
            self._clear_recovery_checkpoints(state, "counterargument")
            self.repository.save(state)
            return await self._integrate_and_review(
                state,
                rerun_initial=False,
                rerun_counterargument=True,
                progress_callback=progress_callback,
                recovery=True,
            )

        try:
            if not self._checkpoint_is_current(state, "final_integration"):
                raise ValueError("final integration belongs to an earlier revision")
            final = FinalIntegratedAnalysis.model_validate(state.final_integration)
            if final.previous_integration_id != initial.integration_id:
                raise ValueError(
                    "final integration does not reference the saved initial integration"
                )
        except Exception:
            self._clear_recovery_checkpoints(state, "final_integration")
            self.repository.save(state)
            return await self._integrate_and_review(
                state,
                rerun_initial=False,
                rerun_counterargument=False,
                progress_callback=progress_callback,
                recovery=True,
            )

        try:
            if not self._checkpoint_is_current(state, "deterministic_validation"):
                raise ValueError("deterministic validation belongs to an earlier revision")
            DeterministicValidationResult.model_validate(state.deterministic_validation)
        except Exception:
            self._clear_recovery_checkpoints(state, "deterministic_validation")
            self.repository.save(state)
            return await self._integrate_and_review(
                state,
                rerun_initial=False,
                rerun_counterargument=False,
                rerun_final=False,
                rerun_validation=True,
                progress_callback=progress_callback,
                recovery=True,
            )

        if state.deliberation_result is not None:
            try:
                DeliberationResult.model_validate(state.deliberation_result)
            except Exception:
                state.deliberation_result = None
                self.repository.save(state)

        if blocked_review_rerecovery:
            self._clear_recovery_checkpoints(state, "quality_review")
            self.repository.save(state)
            await self._emit(
                progress_callback,
                "旧Quality Reviewを無効化し、Quality Reviewer checkpointから再検証",
            )
            return await self._integrate_and_review(
                state,
                rerun_initial=False,
                rerun_counterargument=False,
                rerun_final=False,
                rerun_validation=False,
                refresh_validation_for_review=True,
                progress_callback=progress_callback,
                recovery=True,
            )

        try:
            if not self._checkpoint_is_current(state, "quality_review"):
                raise ValueError("quality review belongs to an earlier revision")
            review = DeliberationQualityReviewOutput.model_validate(state.review_result)
            review_response_id = self._saved_review_response_id(state, review.review_id)
        except Exception:
            self._clear_recovery_checkpoints(state, "quality_review")
            self.repository.save(state)
            await self._emit(progress_callback, "Quality Reviewer checkpointから再開")
            return await self._integrate_and_review(
                state,
                rerun_initial=False,
                rerun_counterargument=False,
                rerun_final=False,
                rerun_validation=False,
                refresh_validation_for_review=True,
                progress_callback=progress_callback,
                recovery=True,
            )

        counterargument = CounterargumentAnalysisResult.model_validate(
            state.counterargument_analysis
        )
        validation = DeterministicValidationResult.model_validate(
            state.deterministic_validation
        )
        try:
            outcome, rerun_initial, rerun_counterargument = await self._apply_review_decision(
                state,
                report,
                final,
                counterargument,
                validation,
                review,
                review_response_id,
                progress_callback,
            )
        except Exception as exc:
            return await self._fail(
                state,
                f"Deliberation recovery could not continue the saved Quality Review: {exc}",
                progress_callback,
            )
        if outcome is not None:
            return outcome
        return await self._integrate_and_review(
            state,
            rerun_initial=rerun_initial,
            rerun_counterargument=rerun_counterargument,
            progress_callback=progress_callback,
            recovery=True,
        )

    def _validate_researcher_handoff(
        self,
        handoff: PMPMessage,
        *,
        allow_revision: bool = False,
    ) -> ResearchReport:
        self.pmp_validator.validate(handoff)
        if handoff.sender_agent_id != "researcher.manager":
            raise ValueError("Researcher handoff sender must be researcher.manager")
        if handoff.receiver_agent_id != self.agent_id:
            raise ValueError("Researcher handoff receiver must be deliberation.manager")
        allowed = {MessageType.RESEARCH_RESULT.value}
        if allow_revision:
            allowed.add(MessageType.RESEARCH_REVISION_RESULT.value)
        if handoff.message_type not in allowed:
            raise ValueError("Researcher handoff has an invalid message_type")
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
        missing = sorted(required - handoff.payload.keys())
        if missing:
            raise ValueError(f"Researcher handoff is missing: {', '.join(missing)}")
        raw_report = handoff.payload.get("research_report")
        if raw_report is None:
            raw_report = {
                field: handoff.payload[field]
                for field in ResearchReport.model_fields
                if field in handoff.payload
            }
        report = ResearchReport.model_validate(raw_report)
        if report.workflow_id != handoff.workflow_id:
            raise ValueError("Research Report workflow_id does not match PMP workflow_id")
        if report.research_report_id != handoff.payload["research_report_id"]:
            raise ValueError("Research Report ID mismatch in Researcher handoff")
        if not report.evidence_items:
            raise ValueError("Research Report contains no evidence_items")
        evidence_ids = [item.evidence_id for item in report.evidence_items]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("Research Report evidence_id values must be unique")
        source_ids = {item.source_id for item in report.evidence_items}
        metadata_ids = {item.source_id for item in report.source_metadata}
        if source_ids - metadata_ids:
            raise ValueError("Research Report has evidence without source_metadata")
        quality = handoff.payload.get("quality_review") or (
            report.review.model_dump(mode="json") if report.review else {}
        )
        if isinstance(quality, BaseModel):
            quality = quality.model_dump(mode="json")
        if quality.get("status") not in {"approved", "approved_with_conditions"}:
            raise ValueError("Research Report did not pass the Researcher Quality Gate")
        return report

    @staticmethod
    def _create_analysis_tasks(report: ResearchReport) -> list[DeliberationAnalysisTask]:
        evidence_ids = [item.evidence_id for item in report.evidence_items]
        question_ids = [item.research_question_id for item in report.research_questions]
        source_type_by_id = {
            item.source_id: str(item.source_type)
            for item in report.source_metadata
        }
        preferred = {
            AnalysisType.ARGUMENT: set(),
            AnalysisType.CAUSAL_STRUCTURAL: {"ACADEMIC", "GOVERNMENT", "INDUSTRY", "EXPERT"},
            AnalysisType.STAKEHOLDER_RESPONSE: {
                "GOVERNMENT",
                "INDUSTRY",
                "NEWS",
                "PUBLIC_OPINION",
                "POLITICIAN",
            },
        }
        tasks: list[DeliberationAnalysisTask] = []
        for analysis_type, agent_id in ANALYSIS_AGENT_MAP.items():
            target_ids = evidence_ids
            categories = preferred[analysis_type]
            if len(evidence_ids) > 50 and categories:
                target_ids = [
                    item.evidence_id
                    for item in report.evidence_items
                    if source_type_by_id.get(item.source_id) in categories
                ][:50]
                if not target_ids:
                    target_ids = evidence_ids[:50]
            elif len(evidence_ids) > 50:
                target_ids = evidence_ids[:50]
            tasks.append(
                DeliberationAnalysisTask(
                    task_id=new_id("delib_task"),
                    analysis_type=analysis_type,
                    target_agent_id=agent_id,
                    research_report_id=report.research_report_id,
                    research_question_ids=question_ids,
                    target_evidence_ids=target_ids,
                    problem_definition=(
                        f"Topic『{report.topic}』について、一般論『{report.general_opinion}』を"
                        "Research Reportの証拠だけで分析する"
                    ),
                    shared_definitions={
                        "topic": report.topic,
                        "general_opinion": report.general_opinion,
                    },
                    geographic_scope=list(report.research_scope),
                    time_scope={"research_scope": list(report.research_scope)},
                    analysis_constraints=[
                        "Research Report外の事実を追加しない",
                        "最終結論や政策選択を行わない",
                        "不確実性と反対証拠を保持する",
                        *(
                            [
                                "固有名詞、人数、割合、統計値、政策名はevidence_idとsource_idの両方へ紐づける",
                                "裏付け不能な具体情報は抽象化するかunknown/unverifiedとresearch gapへ記録する",
                            ]
                            if analysis_type == AnalysisType.STAKEHOLDER_RESPONSE
                            else []
                        ),
                    ],
                    completion_conditions=[
                        "全主要項目にIDがある",
                        "Evidence参照が追跡可能である",
                        "責務範囲外の判断を行わない",
                        *(
                            ["具体情報のverification statusとresearch gapが明示されている"]
                            if analysis_type == AnalysisType.STAKEHOLDER_RESPONSE
                            else []
                        ),
                    ],
                )
            )
        return tasks

    async def _execute_primary_tasks(
        self,
        state: DeliberationWorkflowState,
        tasks: list[DeliberationAnalysisTask],
        *,
        is_revision: bool,
        progress_callback: ProgressCallback | None,
    ) -> int:
        state.status = WorkflowStatus.REVISING if is_revision else WorkflowStatus.DISPATCHING
        state.current_agent_ids = [task.target_agent_id for task in tasks]
        requests = [self._create_analysis_message(state, task, is_revision) for task in tasks]
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
            succeeded = False
            if isinstance(response, BaseException):
                self._record_failure(state, task.target_agent_id, str(response), task.task_id)
            else:
                state.message_history.append(response)
                error = self._validate_analysis_response(task, request, response)
                if error:
                    self._record_failure(state, task.target_agent_id, error, task.task_id)
                else:
                    normalized_result = self._analysis_schema(
                        task.target_agent_id
                    ).model_validate(response.payload)
                    state.analysis_results[task.target_agent_id] = (
                        normalized_result.model_dump(mode="json")
                    )
                    state.checkpoint_revisions[f"primary:{task.target_agent_id}"] = (
                        state.revision_count
                    )
                    if task.target_agent_id not in state.completed_agents:
                        state.completed_agents.append(task.target_agent_id)
                    if task.target_agent_id in state.failed_agents:
                        state.failed_agents.remove(task.target_agent_id)
                    completed += 1
                    succeeded = True
            self.repository.save(state)
            await self._emit(
                progress_callback,
                f"[{index}/{len(tasks)}] {DISPLAY_NAMES[task.target_agent_id]} "
                + ("完了" if succeeded else "失敗"),
            )
        state.current_agent_ids = []
        state.status = WorkflowStatus.RUNNING if completed == len(tasks) else WorkflowStatus.PARTIALLY_COMPLETED
        self.repository.save(state)
        return completed

    def _create_analysis_message(
        self,
        state: DeliberationWorkflowState,
        task: DeliberationAnalysisTask,
        is_revision: bool,
    ) -> PMPMessage:
        return PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=self._latest_parent_message_id(
                state,
                prefer_quality_review=is_revision,
            ),
            sender_agent_id=self.agent_id,
            receiver_agent_id=task.target_agent_id,
            message_type=MessageType.DELIBERATION_TASK_ASSIGNMENT,
            objective="Revise assigned Deliberation analysis" if is_revision else "Perform assigned Deliberation analysis",
            payload=task.model_dump(mode="json"),
            constraints={
                "evidence_bound_analysis": True,
                "final_conclusion_allowed": False,
                "source_traceability_required": True,
                "specific_facts_require_evidence_and_source": (
                    task.target_agent_id
                    == "deliberation.stakeholder_response_analyst"
                ),
                "unverified_specifics_must_be_abstracted_or_research_gaps": (
                    task.target_agent_id
                    == "deliberation.stakeholder_response_analyst"
                ),
            },
            context=PMPContext(
                current_stage="deliberation.primary_analysis",
                previous_stage="researcher",
                next_stage="deliberation.manager",
            ),
            routing=PMPRouting(
                revision_target=task.target_agent_id if is_revision else None,
                reply_required=True,
            ),
            metadata=PMPMetadata(
                status=MessageStatus.REVISION_REQUIRED if is_revision else MessageStatus.QUEUED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )

    def _validate_analysis_response(
        self,
        task: DeliberationAnalysisTask,
        request: PMPMessage,
        response: PMPMessage,
    ) -> str | None:
        error = self._validate_response_envelope(
            request,
            response,
            sender_agent_id=task.target_agent_id,
            expected_type=MessageType.DELIBERATION_TASK_RESULT.value,
        )
        if error:
            return error
        try:
            result = self._analysis_schema(task.target_agent_id).model_validate(response.payload)
        except Exception as exc:
            return f"Invalid analysis payload from {task.target_agent_id}: {exc}"
        if result.task_id != task.task_id:
            return f"Task ID mismatch from {task.target_agent_id}"
        if result.analysis_id == result.task_id:
            return f"{task.target_agent_id} reused task_id as analysis_id"
        unknown = self._collect_evidence_ids(response.payload) - set(task.target_evidence_ids)
        if unknown:
            return f"{task.target_agent_id} referenced evidence outside its task: {sorted(unknown)}"
        return None

    @staticmethod
    def _analysis_schema(agent_id: str):
        return {
            "deliberation.argument_analyst": ArgumentAnalysisResult,
            "deliberation.causal_structural_analyst": CausalStructuralAnalysisResult,
            "deliberation.stakeholder_response_analyst": StakeholderResponseAnalysisResult,
        }[agent_id]

    def _saved_analysis_is_valid(
        self,
        task: DeliberationAnalysisTask,
        payload: dict[str, Any],
    ) -> bool:
        try:
            result = self._analysis_schema(task.target_agent_id).model_validate(payload)
        except Exception:
            return False
        if result.task_id != task.task_id:
            return False
        return not (
            self._collect_evidence_ids(payload) - set(task.target_evidence_ids)
        )

    def _recovery_primary_tasks(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
    ) -> list[DeliberationAnalysisTask]:
        report_evidence_ids = {item.evidence_id for item in report.evidence_items}
        tasks_by_agent: dict[str, DeliberationAnalysisTask] = {}

        def consider(raw: dict[str, Any]) -> None:
            try:
                task = DeliberationAnalysisTask.model_validate(raw)
            except Exception:
                return
            if task.target_agent_id not in PRIMARY_ANALYST_IDS:
                return
            if task.research_report_id != report.research_report_id:
                return
            if set(task.target_evidence_ids) - report_evidence_ids:
                return
            tasks_by_agent[task.target_agent_id] = task

        for raw in state.analysis_tasks:
            consider(raw)
        for message in state.message_history:
            if (
                message.sender_agent_id == self.agent_id
                and message.receiver_agent_id in PRIMARY_ANALYST_IDS
                and message.message_type == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            ):
                consider(message.payload)

        generated = {
            task.target_agent_id: task for task in self._create_analysis_tasks(report)
        }
        return [
            tasks_by_agent.get(agent_id, generated[agent_id])
            for agent_id in PRIMARY_ANALYST_IDS
        ]

    @staticmethod
    def _make_recovery_task(task: DeliberationAnalysisTask) -> DeliberationAnalysisTask:
        raw = task.model_dump(mode="json")
        previous_task_id = task.task_id
        raw["task_id"] = new_id("delib_task")
        raw["revision_context"] = {
            **(task.revision_context or {}),
            "checkpoint_recovery": True,
            "previous_task_id": previous_task_id,
        }
        return DeliberationAnalysisTask.model_validate(raw)

    @staticmethod
    def _replace_analysis_tasks(
        state: DeliberationWorkflowState,
        replacements: list[DeliberationAnalysisTask],
    ) -> None:
        replacement_by_agent = {task.target_agent_id: task for task in replacements}
        retained: list[dict[str, Any]] = []
        replaced: set[str] = set()
        for raw in state.analysis_tasks:
            agent_id = raw.get("target_agent_id")
            replacement = replacement_by_agent.get(agent_id)
            if replacement is not None and agent_id not in replaced:
                retained.append(replacement.model_dump(mode="json"))
                replaced.add(agent_id)
            elif agent_id not in replacement_by_agent:
                retained.append(raw)
        retained.extend(
            task.model_dump(mode="json")
            for agent_id, task in replacement_by_agent.items()
            if agent_id not in replaced
        )
        state.analysis_tasks = retained

    @staticmethod
    def _checkpoint_is_current(state: DeliberationWorkflowState, stage: str) -> bool:
        recorded_revision = state.checkpoint_revisions.get(stage)
        if recorded_revision is None:
            return True
        revision_stage = (
            "final_integration" if stage == "deterministic_validation" else stage
        )
        required_revision = max(
            (
                record.iteration
                for record in state.revision_history
                if revision_stage in record.rerun_stages
            ),
            default=0,
        )
        return recorded_revision >= required_revision

    @staticmethod
    def _clear_recovery_checkpoints(
        state: DeliberationWorkflowState,
        from_stage: str,
    ) -> None:
        checkpoints = [
            ("initial_integration", "initial_integration"),
            ("counterargument", "counterargument_analysis"),
            ("final_integration", "final_integration"),
            ("deterministic_validation", "deterministic_validation"),
            ("quality_review", "review_result"),
        ]
        start = next(
            index for index, (checkpoint, _field) in enumerate(checkpoints)
            if checkpoint == from_stage
        )
        for checkpoint, field in checkpoints[start:]:
            setattr(state, field, None)
            state.checkpoint_revisions.pop(checkpoint, None)
        if from_stage == "quality_review" and state.deliberation_result is not None:
            state.deliberation_result["quality_review"] = None
        if start <= 1:
            if COUNTERARGUMENT_ANALYST_ID in state.completed_agents:
                state.completed_agents.remove(COUNTERARGUMENT_ANALYST_ID)
        state.conclusion_sent = False

    @staticmethod
    def _saved_review_response_id(
        state: DeliberationWorkflowState,
        review_id: str,
    ) -> str:
        for message in reversed(state.message_history):
            if (
                message.sender_agent_id == QUALITY_REVIEWER_ID
                and message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_RESULT.value
                and message.payload.get("review_id") == review_id
            ):
                return message.message_id
        raise ValueError("Saved Quality Review has no matching PMP response")

    async def _integrate_and_review(
        self,
        state: DeliberationWorkflowState,
        *,
        rerun_initial: bool,
        rerun_counterargument: bool,
        progress_callback: ProgressCallback | None,
        rerun_final: bool = True,
        rerun_validation: bool = True,
        refresh_validation_for_review: bool = False,
        recovery: bool = False,
    ) -> DeliberationWorkflowState:
        report = ResearchReport.model_validate(state.research_report)
        while True:
            try:
                if rerun_initial:
                    state.status = WorkflowStatus.INTEGRATING
                    initial = await self.registry.integrate(
                        input_data={
                            "research_report": state.research_report,
                            "primary_analyses": state.analysis_results,
                            "previous_integration": state.initial_integration,
                            "revision_context": self._latest_revision_context(state),
                        },
                        output_schema=InitialIntegratedAnalysis,
                        stage="initial_integration",
                        workflow_id=state.workflow_id,
                        recovery=recovery,
                    )
                    state.initial_integration = initial.model_dump(mode="json")
                    state.checkpoint_revisions["initial_integration"] = state.revision_count
                    self.repository.save(state)
                    await self._emit(progress_callback, "Deliberation Manager初回統合完了")
                else:
                    initial = InitialIntegratedAnalysis.model_validate(state.initial_integration)

                if rerun_initial or rerun_counterargument:
                    counterargument = await self._execute_counterargument(
                        state,
                        report,
                        initial,
                        is_revision=state.revision_count > 0,
                    )
                    state.counterargument_analysis = counterargument.model_dump(mode="json")
                    state.checkpoint_revisions["counterargument"] = state.revision_count
                    if COUNTERARGUMENT_ANALYST_ID not in state.completed_agents:
                        state.completed_agents.append(COUNTERARGUMENT_ANALYST_ID)
                    if COUNTERARGUMENT_ANALYST_ID in state.failed_agents:
                        state.failed_agents.remove(COUNTERARGUMENT_ANALYST_ID)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Counterargument Analyst完了")
                else:
                    counterargument = CounterargumentAnalysisResult.model_validate(
                        state.counterargument_analysis
                    )

                if rerun_final:
                    final = await self.registry.integrate(
                        input_data={
                            "research_report": state.research_report,
                            "primary_analyses": state.analysis_results,
                            "initial_integration": initial.model_dump(mode="json"),
                            "counterargument_analysis": counterargument.model_dump(mode="json"),
                            "previous_final_integration": state.final_integration,
                            "revision_context": self._latest_revision_context(state),
                        },
                        output_schema=FinalIntegratedAnalysis,
                        stage="final_integration",
                        workflow_id=state.workflow_id,
                        recovery=recovery,
                    )
                    self._validate_final_counterargument_dispositions(
                        counterargument,
                        final,
                    )
                    state.final_integration = final.model_dump(mode="json")
                    state.checkpoint_revisions["final_integration"] = state.revision_count
                else:
                    final = FinalIntegratedAnalysis.model_validate(state.final_integration)

                primary_for_review, initial, counterargument, final = (
                    self._prepare_review_artifacts(
                        state,
                        report,
                        initial,
                        counterargument,
                        final,
                    )
                )

                if rerun_validation or refresh_validation_for_review:
                    validation = self.deterministic_validator.validate(
                        report=report,
                        primary_analyses=primary_for_review,
                        initial_integration=initial,
                        counterargument=counterargument,
                        final_integration=final,
                        revision_count=state.revision_count,
                    )
                    if rerun_validation:
                        state.deterministic_validation = validation.model_dump(mode="json")
                        state.checkpoint_revisions["deterministic_validation"] = state.revision_count
                else:
                    validation = DeterministicValidationResult.model_validate(
                        state.deterministic_validation
                    )

                if rerun_final or rerun_validation or state.deliberation_result is None:
                    provisional = self._build_result(state, report, final, counterargument, None)
                    state.deliberation_result = provisional.model_dump(mode="json")
                self.repository.save(state)
                await self._emit(progress_callback, "Manager再統合・決定論的検証完了")
                review, review_response = await self._request_review(
                    state,
                    report,
                    initial,
                    counterargument,
                    final,
                    validation,
                    primary_for_review,
                )
            except Exception as exc:
                return await self._fail(
                    state,
                    f"Deliberation統合またはQuality Reviewに失敗しました: {exc}",
                    progress_callback,
                )

            state.review_result = review.model_dump(mode="json")
            state.checkpoint_revisions["quality_review"] = state.revision_count
            self.repository.save(state)
            outcome, rerun_initial, rerun_counterargument = await self._apply_review_decision(
                state,
                report,
                final,
                counterargument,
                validation,
                review,
                review_response.message_id,
                progress_callback,
            )
            if outcome is not None:
                return outcome
            rerun_final = True
            rerun_validation = True

    async def _apply_review_decision(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        final: FinalIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        validation: DeterministicValidationResult,
        review: DeliberationQualityReviewOutput,
        review_response_id: str,
        progress_callback: ProgressCallback | None,
    ) -> tuple[DeliberationWorkflowState | None, bool, bool]:
        if review.status in {
            QualityGateDecision.APPROVED.value,
            QualityGateDecision.APPROVED_WITH_CONDITIONS.value,
        }:
            if not validation.passed:
                return (
                    await self._block(
                        state,
                        "決定論的Validatorが失敗しているため、LLM承認を採用できません",
                        progress_callback,
                    ),
                    False,
                    False,
                )
            result = self._build_result(state, report, final, counterargument, review)
            state.deliberation_result = result.model_dump(mode="json")
            state.status = WorkflowStatus.APPROVED
            self.repository.save_result(result)
            self.repository.save(state)
            try:
                self._send_to_conclusion(state, result, review_response_id)
            except Exception as exc:
                return (
                    await self._fail(
                        state,
                        f"Conclusion Outboxへの送信に失敗しました: {exc}",
                        progress_callback,
                    ),
                    False,
                    False,
                )
            state.conclusion_sent = True
            state.status = WorkflowStatus.COMPLETED
            state.current_agent_ids = []
            state.completed_at = utc_now()
            state.error = None
            self.repository.save(state)
            await self._emit(progress_callback, f"Quality Reviewer: {review.status}")
            return state, False, False

        if review.status == QualityGateDecision.BLOCKED.value:
            return await self._block(state, review.reason, progress_callback), False, False

        if review.upstream_revision_requests:
            if (
                state.awaiting_upstream_revision
                and state.pending_revision_review_id == review.review_id
            ):
                return state, False, False
            self._store_pending_revision(state, review)
            return (
                await self._request_upstream_revision(
                    state,
                    review,
                    review_response_id,
                    progress_callback,
                ),
                False,
                False,
            )

        if self.demo_safe_mode:
            return (
                await self._block(
                    state,
                    "Demo Safe Mode stopped automatic internal revision; "
                    "no Deliberation Agent was re-dispatched",
                    progress_callback,
                ),
                False,
                False,
            )

        return await self._start_internal_revision(
            state,
            review,
            targets=list(review.revision_targets),
            progress_callback=progress_callback,
        )

    async def _start_internal_revision(
        self,
        state: DeliberationWorkflowState,
        review: DeliberationQualityReviewOutput,
        *,
        targets: list[str],
        progress_callback: ProgressCallback | None,
    ) -> tuple[DeliberationWorkflowState | None, bool, bool]:
        state.revision_count += 1
        rerun_stages = self._revision_stages(targets)
        state.revision_history.append(
            DeliberationRevisionRecord(
                iteration=state.revision_count,
                target_agent_ids=targets,
                findings=[item.model_dump(mode="json") for item in review.findings],
                rerun_stages=rerun_stages,
            )
        )
        # Persist consumption of the pending plan and the single revision count
        # before any provider call, so a process restart cannot execute it twice.
        self.repository.save(state)
        if state.revision_count >= self.max_revisions:
            return (
                await self._block(
                    state,
                    f"Quality Reviewerが{self.max_revisions}回revision_requiredを返したため停止しました",
                    progress_callback,
                ),
                False,
                False,
            )
        primary_targets = [item for item in targets if item in PRIMARY_ANALYST_IDS]
        if primary_targets:
            revision_tasks = self._build_revision_tasks(state, primary_targets, review)
            completed = await self._execute_primary_tasks(
                state,
                revision_tasks,
                is_revision=True,
                progress_callback=progress_callback,
            )
            if completed != len(revision_tasks):
                return (
                    await self._fail(
                        state,
                        "修正対象の一次分析Agentが完了しませんでした",
                        progress_callback,
                    ),
                    False,
                    False,
                )
        rerun_initial = bool(primary_targets or self.agent_id in targets)
        rerun_counterargument = bool(
            rerun_initial or COUNTERARGUMENT_ANALYST_ID in targets
        )
        if not rerun_initial and not rerun_counterargument:
            return (
                await self._fail(
                    state,
                    "revision_requiredを実行可能な依存関係へ解決できませんでした",
                    progress_callback,
                ),
                False,
                False,
            )
        await self._emit(
            progress_callback,
            "Quality Reviewer: revision_required → "
            + ", ".join(targets)
            + f"（{state.revision_count}/{self.max_revisions}）",
        )
        return None, rerun_initial, rerun_counterargument

    async def _execute_counterargument(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        initial: InitialIntegratedAnalysis,
        *,
        is_revision: bool,
    ) -> CounterargumentAnalysisResult:
        key_claim_ids = [
            item.claim_id
            for item in initial.key_claims
        ]
        if not key_claim_ids:
            key_claim_ids = [claim.claim_id for claim in ArgumentAnalysisResult.model_validate(
                state.analysis_results["deliberation.argument_analyst"]
            ).central_claims]
        task = CounterargumentTask(
            task_id=new_id("counter_task"),
            initial_integration_id=initial.integration_id,
            key_claim_ids=key_claim_ids,
            candidate_viewpoint_ids=[item.viewpoint_id for item in initial.candidate_viewpoints],
            evidence_ids=[item.evidence_id for item in report.evidence_items],
            agreements=[item.model_dump(mode="json") for item in initial.agreements],
            conflicts=[item.model_dump(mode="json") for item in initial.conflicts],
            unresolved_items=[
                item.model_dump(mode="json") for item in initial.unresolved_items
            ],
            initial_integration=initial.model_dump(mode="json"),
            research_report=report.model_dump(mode="json"),
            revision_context=self._latest_revision_context(state) if is_revision else None,
        )
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=self._latest_message_id(
                state,
                sender_agent_ids=PRIMARY_ANALYST_IDS,
            ),
            sender_agent_id=self.agent_id,
            receiver_agent_id=COUNTERARGUMENT_ANALYST_ID,
            message_type=MessageType.DELIBERATION_TASK_ASSIGNMENT,
            objective="Challenge the initial integration and prescribe traceable revisions",
            payload=task.model_dump(mode="json"),
            constraints={
                "steelman_required": True,
                "false_balance_check_required": True,
                "final_conclusion_allowed": False,
            },
            context=PMPContext(
                current_stage="deliberation.counterargument",
                previous_stage="deliberation.initial_integration",
                next_stage="deliberation.final_integration",
            ),
            routing=PMPRouting(
                revision_target=COUNTERARGUMENT_ANALYST_ID if is_revision else None,
                reply_required=True,
            ),
            metadata=PMPMetadata(
                status=MessageStatus.REVISION_REQUIRED if is_revision else MessageStatus.QUEUED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        state.current_agent_ids = [COUNTERARGUMENT_ANALYST_ID]
        state.message_history.append(request)
        self.repository.save(state)
        response = await self.registry.get(COUNTERARGUMENT_ANALYST_ID).execute(request)
        state.message_history.append(response)
        state.current_agent_ids = []
        self.repository.save(state)
        error = self._validate_response_envelope(
            request,
            response,
            sender_agent_id=COUNTERARGUMENT_ANALYST_ID,
            expected_type=MessageType.DELIBERATION_TASK_RESULT.value,
        )
        if error:
            self._record_failure(state, COUNTERARGUMENT_ANALYST_ID, error, task.task_id)
            raise ValueError(error)
        result = CounterargumentAnalysisResult.model_validate(response.payload)
        if result.task_id != task.task_id:
            raise ValueError("Counterargument task_id mismatch")
        if result.analysis_id in {result.task_id, initial.integration_id}:
            raise ValueError(
                "Counterargument analysis_id must not reuse task_id or integration_id"
            )
        unrouted = result.unrouted_required_counterargument_ids()
        if unrouted:
            raise ValueError(
                "Counterargument required_revision routing omitted blocking IDs: "
                f"{unrouted}"
            )
        unknown = self._collect_evidence_ids(response.payload) - set(task.evidence_ids)
        if unknown:
            raise ValueError(f"Counterargument references unknown evidence IDs: {sorted(unknown)}")
        return result

    def _prepare_review_artifacts(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        initial: InitialIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        final: FinalIntegratedAnalysis,
    ) -> tuple[
        dict[str, dict[str, Any]],
        InitialIntegratedAnalysis,
        CounterargumentAnalysisResult,
        FinalIntegratedAnalysis,
    ]:
        """Create an in-memory v2 review view without rewriting saved checkpoints."""

        normalized_primary = {
            agent_id: self._analysis_schema(agent_id)
            .model_validate(payload)
            .model_dump(mode="json")
            for agent_id, payload in state.analysis_results.items()
            if agent_id in PRIMARY_ANALYST_IDS
        }
        counter_data = counterargument.model_dump(mode="json")
        counter_data["remaining_uncertainties"] = list(
            dict.fromkeys(
                [
                    *counterargument.remaining_uncertainties,
                    *(
                        item.remaining_uncertainty
                        for item in counterargument.counterarguments
                        if item.remaining_uncertainty
                    ),
                ]
            )
        )
        counterargument = CounterargumentAnalysisResult.model_validate(counter_data)

        source_by_evidence = {
            item.evidence_id: item.source_id for item in report.evidence_items
        }
        initial = self._enrich_traceability_sources(
            initial,
            source_by_evidence,
        )
        final = self._enrich_traceability_sources(final, source_by_evidence)

        final_data = final.model_dump(mode="json")
        dispositions = {
            item["counterargument_id"]: item
            for item in final_data.get("counterargument_dispositions", [])
        }
        changes_by_counterargument: dict[str, list[str]] = {}
        for change in final.integration_changes:
            for counterargument_id in change.source_counterargument_ids:
                changes_by_counterargument.setdefault(counterargument_id, []).append(
                    change.change_id
                )
        for item in counterargument.counterarguments:
            if not item.required_revision or item.counterargument_id in dispositions:
                continue
            change_ids = changes_by_counterargument.get(item.counterargument_id, [])
            if change_ids:
                resolution = "revised"
                rationale = "Legacy integration_changes record this counterargument"
            elif item.research_gap_required:
                resolution = "researcher_return"
                rationale = "Counterargument requires evidence unavailable in Deliberation"
            else:
                resolution = "unresolved"
                rationale = "Legacy checkpoint has no explicit disposition"
            dispositions[item.counterargument_id] = {
                "counterargument_id": item.counterargument_id,
                "resolution": resolution,
                "rationale": rationale,
                "revision_target_agent_ids": item.revision_target_agent_ids,
                "integration_change_ids": change_ids,
                "remaining_uncertainty": item.remaining_uncertainty
                or "Legacy counterargument remains unresolved",
                "research_gap_required": item.research_gap_required,
                "acceptance_conditions": item.acceptance_conditions,
            }
        final_data["counterargument_dispositions"] = list(dispositions.values())
        final = FinalIntegratedAnalysis.model_validate(final_data)
        return normalized_primary, initial, counterargument, final

    @staticmethod
    def _enrich_traceability_sources(
        artifact: InitialIntegratedAnalysis | FinalIntegratedAnalysis,
        source_by_evidence: dict[str, str],
    ) -> InitialIntegratedAnalysis | FinalIntegratedAnalysis:
        data = artifact.model_dump(mode="json")
        for entry in data.get("traceability_index", []):
            entry["source_ids"] = list(
                dict.fromkeys(
                    [
                        *entry.get("source_ids", []),
                        *(
                            source_by_evidence[evidence_id]
                            for evidence_id in entry.get("evidence_ids", [])
                            if evidence_id in source_by_evidence
                        ),
                    ]
                )
            )
        return type(artifact).model_validate(data)

    @staticmethod
    def _validate_final_counterargument_dispositions(
        counterargument: CounterargumentAnalysisResult,
        final: FinalIntegratedAnalysis,
    ) -> None:
        disposition_ids = {
            item.counterargument_id for item in final.counterargument_dispositions
        }
        required_ids = {
            item.counterargument_id
            for item in counterargument.counterarguments
            if item.required_revision
        }
        missing = sorted(required_ids - disposition_ids)
        if missing:
            raise ValueError(
                "Final integration omitted blocking counterargument dispositions: "
                f"{missing}"
            )

    @staticmethod
    def _latest_message_id(
        state: DeliberationWorkflowState,
        *,
        sender_agent_ids: set[str] | None = None,
    ) -> str | None:
        for message in reversed(state.message_history):
            if sender_agent_ids is None or message.sender_agent_id in sender_agent_ids:
                return message.message_id
        return None

    @classmethod
    def _latest_parent_message_id(
        cls,
        state: DeliberationWorkflowState,
        *,
        prefer_quality_review: bool,
    ) -> str | None:
        if prefer_quality_review:
            review_id = cls._latest_message_id(
                state,
                sender_agent_ids={QUALITY_REVIEWER_ID},
            )
            if review_id:
                return review_id
        return cls._latest_message_id(
            state,
            sender_agent_ids={"researcher.manager"},
        )

    @staticmethod
    def _build_pmp_routing_trace(
        state: DeliberationWorkflowState,
        review_request: PMPMessage,
    ) -> list[dict[str, Any]]:
        messages = [*state.message_history, review_request]
        response_by_parent = {
            message.parent_message_id: message
            for message in state.message_history
            if message.parent_message_id
        }
        quality_review_attempts: dict[str, int] = {}
        attempt = -1
        for message in messages:
            if (
                message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            ):
                attempt += 1
                quality_review_attempts[message.message_id] = attempt

        trace: list[dict[str, Any]] = []
        last_researcher_message_id: str | None = None
        last_primary_result_id: str | None = None
        last_counterargument_result_id: str | None = None
        last_quality_review_response_id: str | None = None
        for message in messages:
            if message.workflow_id != state.workflow_id:
                continue
            involved = (
                message.sender_agent_id.startswith("deliberation.")
                or message.receiver_agent_id.startswith("deliberation.")
            )
            if not involved:
                continue
            status = message.metadata.status
            if hasattr(status, "value"):
                status = status.value
            parent_message_id = message.parent_message_id
            if parent_message_id is None:
                if (
                    message.message_type
                    == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
                    and message.receiver_agent_id in PRIMARY_ANALYST_IDS
                ):
                    parent_message_id = last_researcher_message_id
                elif (
                    message.message_type
                    == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
                    and message.receiver_agent_id == COUNTERARGUMENT_ANALYST_ID
                ):
                    parent_message_id = last_primary_result_id
                elif (
                    message.message_type
                    == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
                ):
                    parent_message_id = (
                        last_quality_review_response_id
                        or last_counterargument_result_id
                    )

            paired_response = response_by_parent.get(message.message_id)
            if paired_response is not None and message.message_id != review_request.message_id:
                paired_status = paired_response.metadata.status
                if hasattr(paired_status, "value"):
                    paired_status = paired_status.value
                status = "failed" if paired_status == MessageStatus.FAILED.value else "completed"

            retry_count = message.metadata.retry_count
            stage = message.context.current_stage or str(message.message_type)
            if message.message_id in quality_review_attempts:
                retry_count = quality_review_attempts[message.message_id]
                if message.message_id == review_request.message_id:
                    status = MessageStatus.QUEUED.value
                    stage = f"deliberation.quality_review.attempt_{retry_count + 1}.current"
                elif status == MessageStatus.COMPLETED.value:
                    status = "superseded"
                    stage = f"deliberation.quality_review.attempt_{retry_count + 1}.superseded"
                else:
                    stage = f"deliberation.quality_review.attempt_{retry_count + 1}.failed"
            elif parent_message_id in quality_review_attempts:
                retry_count = quality_review_attempts[parent_message_id]
                if message.message_type == MessageType.ERROR.value:
                    status = MessageStatus.FAILED.value
                    stage = f"deliberation.quality_review.attempt_{retry_count + 1}.failed"
                else:
                    status = "superseded"
                    stage = f"deliberation.quality_review.attempt_{retry_count + 1}.superseded"

            trace.append(
                {
                    "workflow_id": message.workflow_id,
                    "message_id": message.message_id,
                    "parent_message_id": parent_message_id,
                    "sender_agent_id": message.sender_agent_id,
                    "receiver_agent_id": message.receiver_agent_id,
                    "message_type": str(message.message_type),
                    "status": str(status),
                    "revision_target": message.routing.revision_target,
                    "retry_count": retry_count,
                    "execution_order": len(trace) + 1,
                    "stage": stage,
                }
            )
            if message.sender_agent_id == "researcher.manager":
                last_researcher_message_id = message.message_id
            if (
                message.sender_agent_id in PRIMARY_ANALYST_IDS
                and message.message_type == MessageType.DELIBERATION_TASK_RESULT.value
            ):
                last_primary_result_id = message.message_id
            if (
                message.sender_agent_id == COUNTERARGUMENT_ANALYST_ID
                and message.message_type == MessageType.DELIBERATION_TASK_RESULT.value
            ):
                last_counterargument_result_id = message.message_id
            if message.sender_agent_id == QUALITY_REVIEWER_ID:
                last_quality_review_response_id = message.message_id
        return trace

    @staticmethod
    def _build_checkpoint_trace(
        state: DeliberationWorkflowState,
        initial: InitialIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        final: FinalIntegratedAnalysis,
        primary_analyses: dict[str, dict[str, Any]],
        validation: DeterministicValidationResult,
        review_request: PMPMessage,
    ) -> list[dict[str, Any]]:
        primary_message_ids = [
            message.message_id
            for message in state.message_history
            if message.sender_agent_id in PRIMARY_ANALYST_IDS
            and message.message_type == MessageType.DELIBERATION_TASK_RESULT.value
        ]
        counter_message_ids = [
            message.message_id
            for message in state.message_history
            if message.sender_agent_id == COUNTERARGUMENT_ANALYST_ID
            and message.message_type == MessageType.DELIBERATION_TASK_RESULT.value
        ]
        primary_analysis_ids = [
            payload.get("analysis_id", "")
            for agent_id, payload in primary_analyses.items()
            if agent_id in PRIMARY_ANALYST_IDS and payload.get("analysis_id")
        ]
        stages = [
            ("primary_analyses", primary_analysis_ids, primary_message_ids),
            ("initial_integration", [initial.integration_id], primary_message_ids),
            (
                "counterargument",
                [counterargument.analysis_id],
                counter_message_ids,
            ),
            ("final_integration", [final.integration_id], counter_message_ids),
            (
                "deterministic_validation",
                [
                    *validation.validation_targets.analysis_ids,
                    *validation.validation_targets.integration_ids,
                ],
                [],
            ),
            ("quality_review_request", [review_request.message_id], [review_request.message_id]),
        ]
        return [
            {
                "execution_order": index,
                "stage": stage,
                "status": "completed" if stage != "quality_review_request" else "queued",
                "artifact_ids": artifact_ids,
                "source_message_ids": source_message_ids,
                "revision_iteration": state.revision_count,
            }
            for index, (stage, artifact_ids, source_message_ids) in enumerate(
                stages,
                start=1,
            )
        ]

    async def _request_review(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        initial: InitialIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        final: FinalIntegratedAnalysis,
        validation: DeterministicValidationResult,
        primary_analyses: dict[str, dict[str, Any]],
    ) -> tuple[DeliberationQualityReviewOutput, PMPMessage]:
        state.status = WorkflowStatus.REVIEWING
        review_task_id = new_id("delib_review_task")
        request_stub = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=(
                self._latest_message_id(
                    state,
                    sender_agent_ids={QUALITY_REVIEWER_ID},
                )
                or self._latest_message_id(
                    state,
                    sender_agent_ids={COUNTERARGUMENT_ANALYST_ID},
                )
            ),
            sender_agent_id=self.agent_id,
            receiver_agent_id=QUALITY_REVIEWER_ID,
            message_type=MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT,
            objective="Review Deliberation completeness, traceability, boundaries, and Conclusion readiness",
            payload={"task_id": review_task_id},
            constraints={
                "do_not_reanalyze": True,
                "deterministic_findings_cannot_be_ignored": True,
                "route_revisions_only_through_manager": True,
            },
            context=PMPContext(
                current_stage="deliberation.quality_review",
                previous_stage="deliberation.final_integration",
                next_stage="deliberation.manager",
            ),
            metadata=PMPMetadata(
                status=MessageStatus.QUEUED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        review_input = DeliberationQualityReviewInput(
            task_id=review_task_id,
            research_report=report.model_dump(mode="json"),
            primary_analyses=primary_analyses,
            initial_integration=initial,
            counterargument_analysis=counterargument,
            final_integration=final,
            deterministic_validation=validation,
            pmp_routing_trace=self._build_pmp_routing_trace(
                state,
                request_stub,
            ),
            checkpoint_trace=self._build_checkpoint_trace(
                state,
                initial,
                counterargument,
                final,
                primary_analyses,
                validation,
                request_stub,
            ),
            failed_agent_ids=state.failed_agents,
            limitations=[str(item.get("message", item)) for item in state.limitations],
            revision_context=self._latest_revision_context(state),
        )
        request_data = request_stub.model_dump(mode="json")
        request_data["payload"] = review_input.model_dump(mode="json")
        request = PMPMessage.model_validate(request_data)
        state.current_agent_ids = [QUALITY_REVIEWER_ID]
        state.message_history.append(request)
        self.repository.save(state)
        response = await self.registry.get(QUALITY_REVIEWER_ID).execute(request)
        state.message_history.append(response)
        state.current_agent_ids = []
        self.repository.save(state)
        error = self._validate_response_envelope(
            request,
            response,
            sender_agent_id=QUALITY_REVIEWER_ID,
            expected_type=MessageType.DELIBERATION_QUALITY_REVIEW_RESULT.value,
        )
        if error:
            raise ValueError(error)
        return DeliberationQualityReviewOutput.model_validate(response.payload), response

    @staticmethod
    def _store_pending_revision(
        state: DeliberationWorkflowState,
        review: DeliberationQualityReviewOutput,
    ) -> None:
        state.pending_revision_targets = list(review.revision_targets)
        state.pending_revision_finding_ids = [item.finding_id for item in review.findings]
        state.pending_upstream_revision_request_ids = [
            item.revision_request_id for item in review.upstream_revision_requests
        ]
        state.pending_revision_scope = str(review.revision_scope)
        state.pending_revision_iteration = state.revision_count + 1
        state.pending_revision_review_id = review.review_id
        state.awaiting_upstream_revision = True

    @staticmethod
    def _clear_pending_revision(state: DeliberationWorkflowState) -> None:
        state.pending_revision_targets = []
        state.pending_revision_finding_ids = []
        state.pending_upstream_revision_request_ids = []
        state.pending_revision_scope = None
        state.pending_revision_iteration = None
        state.pending_revision_review_id = None
        state.awaiting_upstream_revision = False

    @staticmethod
    def _pending_revision_review(
        state: DeliberationWorkflowState,
    ) -> DeliberationQualityReviewOutput:
        if state.review_result is None:
            raise ValueError("Waiting Deliberation workflow has no saved Quality Review")
        review = DeliberationQualityReviewOutput.model_validate(state.review_result)
        if (
            state.pending_revision_review_id is not None
            and state.pending_revision_review_id != review.review_id
        ):
            raise ValueError("Pending revision plan does not match the saved Quality Review")
        return review

    async def _request_upstream_revision(
        self,
        state: DeliberationWorkflowState,
        review: DeliberationQualityReviewOutput,
        parent_message_id: str,
        progress_callback: ProgressCallback | None,
    ) -> DeliberationWorkflowState:
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=parent_message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="researcher.manager",
            message_type=MessageType.RESEARCH_REVISION_REQUEST,
            objective="Collect evidence required to continue Deliberation",
            payload={
                "research_report_id": state.research_report["research_report_id"],
                "revision_requests": [
                    item.model_dump(mode="json") for item in review.upstream_revision_requests
                ],
                "quality_review_id": review.review_id,
            },
            constraints={
                "preserve_research_plan_scope": True,
                "return_updated_research_report": True,
            },
            context=PMPContext(
                current_stage="deliberation.upstream_revision",
                previous_stage="deliberation.quality_review",
                next_stage="researcher",
            ),
            routing=PMPRouting(revision_target="researcher.manager", reply_required=True),
            metadata=PMPMetadata(
                status=MessageStatus.REVISION_REQUIRED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        self.pmp_validator.validate(message)
        self.repository.save_researcher_revision_outbox(message)
        state.message_history.append(message)
        state.upstream_revision_count += 1
        state.upstream_revision_history.append(
            UpstreamRevisionRecord(
                iteration=state.upstream_revision_count,
                request_message_id=message.message_id,
                requests=[item.model_dump(mode="json") for item in review.upstream_revision_requests],
            )
        )
        state.status = WorkflowStatus.WAITING_UPSTREAM_REVISION
        state.error = None
        self.repository.save(state)
        await self._emit(progress_callback, "Researcherへ追加調査を要求し、Workflowを待機状態にしました")
        return state

    def _build_revision_tasks(
        self,
        state: DeliberationWorkflowState,
        targets: list[str],
        review: DeliberationQualityReviewOutput,
    ) -> list[DeliberationAnalysisTask]:
        originals = [DeliberationAnalysisTask.model_validate(item) for item in state.analysis_tasks]
        selected: list[DeliberationAnalysisTask] = []
        for target in targets:
            original = next((item for item in originals if item.target_agent_id == target), None)
            if original is None:
                raise ValueError(f"No original analysis task for revision target: {target}")
            data = original.model_dump(mode="json")
            data["task_id"] = new_id("delib_task")
            data["revision_context"] = {
                "iteration": state.revision_count,
                "review_id": review.review_id,
                "findings": [
                    item.model_dump(mode="json")
                    for item in review.findings
                    if target in item.affected_agent_ids or not item.affected_agent_ids
                ],
            }
            selected.append(DeliberationAnalysisTask.model_validate(data))
        return selected

    def _build_result(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        final: FinalIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        review: DeliberationQualityReviewOutput | None,
    ) -> DeliberationResult:
        argument = self._optional_analysis(
            state,
            "deliberation.argument_analyst",
            ArgumentAnalysisResult,
        )
        causal = self._optional_analysis(
            state,
            "deliberation.causal_structural_analyst",
            CausalStructuralAnalysisResult,
        )
        stakeholder = self._optional_analysis(
            state,
            "deliberation.stakeholder_response_analyst",
            StakeholderResponseAnalysisResult,
        )
        claim_structure = (
            [item.model_dump(mode="json") for item in argument.central_claims]
            if argument
            else [item.model_dump(mode="json") for item in final.key_claims]
        )
        key_assumptions = (
            [item.model_dump(mode="json") for item in argument.premises]
            if argument
            else []
        )
        evidence_relationships: list[dict[str, Any]] = []
        if argument:
            evidence_relationships.extend(
                item.model_dump(mode="json") for item in argument.evidence_mappings
            )
        if causal:
            evidence_relationships.extend(
                item.model_dump(mode="json") for item in causal.evidence_mappings
            )
        if stakeholder:
            evidence_relationships.extend(
                item.model_dump(mode="json") for item in stakeholder.evidence_mappings
            )
        source_traceability = [
            {
                "evidence_id": item.evidence_id,
                "source_id": item.source_id,
                "research_question_ids": item.research_question_ids,
            }
            for item in report.evidence_items
        ]
        analysis_traceability = []
        for agent_id, payload in state.analysis_results.items():
            analysis_traceability.append(
                {
                    "analysis_id": payload.get("analysis_id"),
                    "agent_id": agent_id,
                    "evidence_ids": sorted(self._collect_evidence_ids(payload)),
                }
            )
        analysis_traceability.append(
            {
                "analysis_id": counterargument.analysis_id,
                "agent_id": COUNTERARGUMENT_ANALYST_ID,
                "evidence_ids": sorted(
                    self._collect_evidence_ids(counterargument.model_dump(mode="json"))
                ),
            }
        )
        limitation_strings = (
            report.research_limitations
            + final.limitations
            + [str(item.get("message", item)) for item in state.limitations]
            + (review.limitations_to_disclose if review else [])
        )
        if causal:
            structural_factors = [
                item.model_dump(mode="json") for item in causal.structural_factors
            ]
        else:
            structural_factors = [
                item.model_dump(mode="json")
                if isinstance(item, BaseModel)
                else item
                if isinstance(item, dict)
                else {
                    "factor_id": new_id("structural_factor"),
                    "description": str(item),
                    "evidence_ids": [],
                    "status": "UNVERIFIED_DUE_TO_PARTIAL_FAILURE",
                }
                for item in final.causal_structure.structural_factors
            ]
            if not structural_factors:
                structural_factors = [
                    {
                        "factor_id": new_id("structural_factor"),
                        "description": "Causal & Structural Analystの失敗により詳細未検証",
                        "evidence_ids": [],
                        "status": "UNVERIFIED_DUE_TO_PARTIAL_FAILURE",
                    }
                ]
        return DeliberationResult(
            deliberation_result_id=(
                state.deliberation_result.get("deliberation_result_id")
                if state.deliberation_result
                else new_id("deliberation_result")
            ),
            workflow_id=state.workflow_id,
            research_report_id=report.research_report_id,
            research_plan_id=report.research_plan_id,
            topic=report.topic,
            general_opinion=report.general_opinion,
            problem_definition=final.problem_definition.model_dump(mode="json"),
            claim_structure=claim_structure,
            key_assumptions=key_assumptions,
            evidence_relationships=evidence_relationships,
            causal_model=final.causal_structure.model_dump(mode="json"),
            structural_factors=structural_factors,
            stakeholder_structure=final.stakeholder_structure.model_dump(mode="json"),
            existing_response_evaluation=[
                item.model_dump(mode="json")
                for item in final.existing_response_assessment
            ],
            counterarguments=[item.model_dump(mode="json") for item in counterargument.counterarguments],
            alternative_interpretations=[
                item.model_dump(mode="json")
                for item in counterargument.alternative_interpretations
            ],
            trade_offs=[item.model_dump(mode="json") for item in final.tradeoffs],
            uncertainties=list(
                dict.fromkeys(final.uncertainties + counterargument.remaining_uncertainties)
            ),
            analysis_perspectives=final.major_viewpoints,
            unresolved_issues=[
                item.model_dump(mode="json") for item in final.unresolved_questions
            ],
            research_gaps=[item.model_dump(mode="json") for item in report.evidence_gaps],
            source_traceability=source_traceability,
            analysis_traceability=analysis_traceability,
            claim_traceability=final.traceability_index,
            limitations=list(dict.fromkeys(limitation_strings)),
            quality_review=review.model_dump(mode="json") if review else None,
        )

    def _send_to_conclusion(
        self,
        state: DeliberationWorkflowState,
        result: DeliberationResult,
        parent_message_id: str,
    ) -> None:
        payload = result.model_dump(mode="json")
        payload["deliberation_result"] = result.model_dump(mode="json")
        self._validate_conclusion_handoff(payload)
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=parent_message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="conclusion.manager",
            message_type=MessageType.DELIBERATION_RESULT,
            objective="Provide quality-approved Deliberation Result to Conclusion",
            payload=payload,
            context=PMPContext(
                current_stage="deliberation",
                previous_stage="deliberation.quality_reviewer",
                next_stage="conclusion",
            ),
            metadata=PMPMetadata(
                status=MessageStatus.COMPLETED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        self.pmp_validator.validate(message)
        self.repository.save_conclusion_outbox(message)
        state.message_history.append(message)

    @staticmethod
    def _validate_conclusion_handoff(payload: dict[str, Any]) -> None:
        required = {
            "problem_definition",
            "claim_structure",
            "key_assumptions",
            "evidence_relationships",
            "causal_model",
            "structural_factors",
            "stakeholder_structure",
            "existing_response_evaluation",
            "counterarguments",
            "alternative_interpretations",
            "trade_offs",
            "uncertainties",
            "analysis_perspectives",
            "unresolved_issues",
            "research_gaps",
            "source_traceability",
            "claim_traceability",
            "quality_review",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(f"Deliberation→Conclusion handoff is missing: {', '.join(missing)}")
        if len(payload["analysis_perspectives"]) > 3:
            raise ValueError("Conclusion handoff cannot contain more than three viewpoints")
        for item in payload["source_traceability"]:
            if not item.get("evidence_id") or not item.get("source_id"):
                raise ValueError("source_traceability requires evidence_id and source_id")
        for item in payload.get("analysis_traceability", []):
            if not item.get("analysis_id"):
                raise ValueError("analysis_traceability requires analysis_id")
        if not payload.get("claim_traceability"):
            raise ValueError("claim_traceability is required for Conclusion handoff")

    def _validate_response_envelope(
        self,
        request: PMPMessage,
        response: PMPMessage,
        *,
        sender_agent_id: str,
        expected_type: str,
    ) -> str | None:
        try:
            self.pmp_validator.validate(response)
        except Exception as exc:
            return f"Invalid PMP response from {sender_agent_id}: {exc}"
        if response.workflow_id != request.workflow_id:
            return f"Workflow ID mismatch from {sender_agent_id}"
        if response.parent_message_id != request.message_id:
            return f"Parent message ID mismatch from {sender_agent_id}"
        if response.sender_agent_id != sender_agent_id or response.receiver_agent_id != self.agent_id:
            return f"Invalid routing in response from {sender_agent_id}"
        if response.message_type == MessageType.ERROR.value:
            return f"{sender_agent_id}: {response.payload.get('message', 'unknown error')}"
        if response.message_type != expected_type:
            return f"Unexpected message type from {sender_agent_id}: {response.message_type}"
        return None

    @staticmethod
    def _record_failure(
        state: DeliberationWorkflowState,
        agent_id: str,
        message: str,
        task_id: str,
    ) -> None:
        if agent_id not in state.failed_agents:
            state.failed_agents.append(agent_id)
        state.limitations.append(
            {"stage": "analysis", "task_id": task_id, "agent_id": agent_id, "message": message}
        )

    @staticmethod
    def _optional_analysis(state, agent_id: str, schema):
        payload = state.analysis_results.get(agent_id)
        return schema.model_validate(payload) if payload else None

    @staticmethod
    def _collect_evidence_ids(value: Any) -> set[str]:
        return DeliberationValidator._collect_evidence_ids(value)

    @staticmethod
    def _latest_revision_context(state: DeliberationWorkflowState) -> dict[str, Any] | None:
        return state.revision_history[-1].model_dump(mode="json") if state.revision_history else None

    @staticmethod
    def _revision_stages(targets: list[str]) -> list[str]:
        stages: list[str] = []
        if any(target in PRIMARY_ANALYST_IDS or target == "deliberation.manager" for target in targets):
            stages.extend(
                [
                    "initial_integration",
                    "counterargument",
                    "final_integration",
                    "deterministic_validation",
                    "quality_review",
                ]
            )
        elif COUNTERARGUMENT_ANALYST_ID in targets:
            stages.extend(
                [
                    "counterargument",
                    "final_integration",
                    "deterministic_validation",
                    "quality_review",
                ]
            )
        return stages

    async def _block(
        self,
        state: DeliberationWorkflowState,
        message: str,
        callback: ProgressCallback | None,
    ) -> DeliberationWorkflowState:
        state.status = WorkflowStatus.BLOCKED
        state.error = {"message": message}
        state.current_agent_ids = []
        state.completed_at = utc_now()
        self.repository.save(state)
        await self._emit(callback, f"Deliberation Workflow停止: {message}")
        return state

    async def _fail(
        self,
        state: DeliberationWorkflowState,
        message: str,
        callback: ProgressCallback | None,
    ) -> DeliberationWorkflowState:
        state.status = WorkflowStatus.FAILED
        state.error = {"message": message}
        state.current_agent_ids = []
        state.completed_at = utc_now()
        self.repository.save(state)
        await self._emit(callback, f"Deliberation Workflow失敗: {message}")
        return state

    @staticmethod
    async def _emit(callback: ProgressCallback | None, message: str) -> None:
        if callback is None:
            return
        result = callback(message)
        if inspect.isawaitable(result):
            await result
