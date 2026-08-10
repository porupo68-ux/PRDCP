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
    RevisionScope,
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
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.max_revisions = max_revisions
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
        if runtime_config.revision_limit is not None:
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
        state.researcher_handoff = handoff.model_dump(mode="json")
        state.research_report = report.model_dump(mode="json")
        state.analysis_tasks = [task.model_dump(mode="json") for task in tasks]
        state.analysis_results = {}
        state.initial_integration = None
        state.counterargument_analysis = None
        state.final_integration = None
        state.deterministic_validation = None
        state.deliberation_result = None
        state.review_result = None
        state.completed_agents = []
        state.failed_agents = []
        state.current_agent_ids = []
        state.revision_count = 0
        state.error = None
        state.message_history.append(handoff)
        self.repository.save(state)
        await self._emit(progress_callback, "Researcher追加調査結果を受領し、Deliberationを再開します")
        completed = await self._execute_primary_tasks(
            state,
            tasks,
            is_revision=True,
            progress_callback=progress_callback,
        )
        if completed < 2:
            return await self._block(
                state,
                "再開後の一次分析が2系統未満のため停止しました",
                progress_callback,
            )
        return await self._integrate_and_review(
            state,
            rerun_initial=True,
            rerun_counterargument=True,
            progress_callback=progress_callback,
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
        metadata_ids = {str(item.get("source_id")) for item in report.source_metadata}
        if source_ids - metadata_ids:
            raise ValueError("Research Report has evidence without source_metadata")
        quality = handoff.payload.get("quality_review") or report.review or {}
        if quality.get("status") not in {"approved", "approved_with_conditions"}:
            raise ValueError("Research Report did not pass the Researcher Quality Gate")
        return report

    @staticmethod
    def _create_analysis_tasks(report: ResearchReport) -> list[DeliberationAnalysisTask]:
        evidence_ids = [item.evidence_id for item in report.evidence_items]
        question_ids = [item.research_question_id for item in report.research_questions]
        source_type_by_id = {
            str(item.get("source_id")): str(item.get("source_type", ""))
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
                    ],
                    completion_conditions=[
                        "全主要項目にIDがある",
                        "Evidence参照が追跡可能である",
                        "責務範囲外の判断を行わない",
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
                    state.analysis_results[task.target_agent_id] = response.payload
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
            sender_agent_id=self.agent_id,
            receiver_agent_id=task.target_agent_id,
            message_type=MessageType.DELIBERATION_TASK_ASSIGNMENT,
            objective="Revise assigned Deliberation analysis" if is_revision else "Perform assigned Deliberation analysis",
            payload=task.model_dump(mode="json"),
            constraints={
                "evidence_bound_analysis": True,
                "final_conclusion_allowed": False,
                "source_traceability_required": True,
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
        schema_by_agent = {
            "deliberation.argument_analyst": ArgumentAnalysisResult,
            "deliberation.causal_structural_analyst": CausalStructuralAnalysisResult,
            "deliberation.stakeholder_response_analyst": StakeholderResponseAnalysisResult,
        }
        error = self._validate_response_envelope(
            request,
            response,
            sender_agent_id=task.target_agent_id,
            expected_type=MessageType.DELIBERATION_TASK_RESULT.value,
        )
        if error:
            return error
        try:
            result = schema_by_agent[task.target_agent_id].model_validate(response.payload)
        except Exception as exc:
            return f"Invalid analysis payload from {task.target_agent_id}: {exc}"
        if result.task_id != task.task_id:
            return f"Task ID mismatch from {task.target_agent_id}"
        unknown = self._collect_evidence_ids(response.payload) - set(task.target_evidence_ids)
        if unknown:
            return f"{task.target_agent_id} referenced evidence outside its task: {sorted(unknown)}"
        return None

    async def _integrate_and_review(
        self,
        state: DeliberationWorkflowState,
        *,
        rerun_initial: bool,
        rerun_counterargument: bool,
        progress_callback: ProgressCallback | None,
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
                    )
                    state.initial_integration = initial.model_dump(mode="json")
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
                )
                state.final_integration = final.model_dump(mode="json")
                validation = self.deterministic_validator.validate(
                    report=report,
                    primary_analyses=state.analysis_results,
                    initial_integration=initial,
                    counterargument=counterargument,
                    final_integration=final,
                    revision_count=state.revision_count,
                )
                state.deterministic_validation = validation.model_dump(mode="json")
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
                )
            except Exception as exc:
                return await self._fail(
                    state,
                    f"Deliberation統合またはQuality Reviewに失敗しました: {exc}",
                    progress_callback,
                )

            state.review_result = review.model_dump(mode="json")
            self.repository.save(state)

            if review.status in {
                QualityGateDecision.APPROVED.value,
                QualityGateDecision.APPROVED_WITH_CONDITIONS.value,
            }:
                if not validation.passed:
                    return await self._block(
                        state,
                        "決定論的Validatorが失敗しているため、LLM承認を採用できません",
                        progress_callback,
                    )
                result = self._build_result(state, report, final, counterargument, review)
                state.deliberation_result = result.model_dump(mode="json")
                state.status = WorkflowStatus.APPROVED
                self.repository.save_result(result)
                self.repository.save(state)
                try:
                    self._send_to_conclusion(state, result, review_response.message_id)
                except Exception as exc:
                    return await self._fail(
                        state,
                        f"Conclusion Outboxへの送信に失敗しました: {exc}",
                        progress_callback,
                    )
                state.conclusion_sent = True
                state.status = WorkflowStatus.COMPLETED
                state.current_agent_ids = []
                state.completed_at = utc_now()
                self.repository.save(state)
                await self._emit(progress_callback, f"Quality Reviewer: {review.status}")
                return state

            if review.status == QualityGateDecision.BLOCKED.value:
                return await self._block(state, review.reason, progress_callback)

            if review.revision_scope == RevisionScope.RESEARCHER_RETURN.value:
                return await self._request_upstream_revision(
                    state,
                    review,
                    review_response.message_id,
                    progress_callback,
                )

            state.revision_count += 1
            rerun_stages = self._revision_stages(review.revision_targets)
            state.revision_history.append(
                DeliberationRevisionRecord(
                    iteration=state.revision_count,
                    target_agent_ids=review.revision_targets,
                    findings=[item.model_dump(mode="json") for item in review.findings],
                    rerun_stages=rerun_stages,
                )
            )
            if state.revision_count >= self.max_revisions:
                return await self._block(
                    state,
                    f"Quality Reviewerが{self.max_revisions}回revision_requiredを返したため停止しました",
                    progress_callback,
                )
            primary_targets = [item for item in review.revision_targets if item in PRIMARY_ANALYST_IDS]
            if primary_targets:
                revision_tasks = self._build_revision_tasks(state, primary_targets, review)
                completed = await self._execute_primary_tasks(
                    state,
                    revision_tasks,
                    is_revision=True,
                    progress_callback=progress_callback,
                )
                if completed != len(revision_tasks):
                    return await self._fail(
                        state,
                        "修正対象の一次分析Agentが完了しませんでした",
                        progress_callback,
                    )
            rerun_initial = bool(primary_targets or self.agent_id in review.revision_targets)
            rerun_counterargument = bool(
                rerun_initial or COUNTERARGUMENT_ANALYST_ID in review.revision_targets
            )
            if not rerun_initial and not rerun_counterargument:
                return await self._fail(
                    state,
                    "revision_requiredを実行可能な依存関係へ解決できませんでした",
                    progress_callback,
                )
            await self._emit(
                progress_callback,
                "Quality Reviewer: revision_required → "
                + ", ".join(review.revision_targets)
                + f"（{state.revision_count}/{self.max_revisions}）",
            )

    async def _execute_counterargument(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        initial: InitialIntegratedAnalysis,
        *,
        is_revision: bool,
    ) -> CounterargumentAnalysisResult:
        key_claim_ids = [
            str(item.get("claim_id") or item.get("id"))
            for item in initial.key_claims
            if item.get("claim_id") or item.get("id")
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
            agreements=initial.agreements,
            conflicts=initial.conflicts,
            unresolved_items=initial.unresolved_items,
            initial_integration=initial.model_dump(mode="json"),
            research_report=report.model_dump(mode="json"),
            revision_context=self._latest_revision_context(state) if is_revision else None,
        )
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
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
        unknown = self._collect_evidence_ids(response.payload) - set(task.evidence_ids)
        if unknown:
            raise ValueError(f"Counterargument references unknown evidence IDs: {sorted(unknown)}")
        return result

    async def _request_review(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        initial: InitialIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        final: FinalIntegratedAnalysis,
        validation: DeterministicValidationResult,
    ) -> tuple[DeliberationQualityReviewOutput, PMPMessage]:
        state.status = WorkflowStatus.REVIEWING
        review_input = DeliberationQualityReviewInput(
            research_report=report.model_dump(mode="json"),
            primary_analyses=state.analysis_results,
            initial_integration=initial,
            counterargument_analysis=counterargument,
            final_integration=final,
            deterministic_validation=validation,
            failed_agent_ids=state.failed_agents,
            limitations=[str(item.get("message", item)) for item in state.limitations],
            revision_context=self._latest_revision_context(state),
        )
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=QUALITY_REVIEWER_ID,
            message_type=MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT,
            objective="Review Deliberation completeness, traceability, boundaries, and Conclusion readiness",
            payload=review_input.model_dump(mode="json"),
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
            else final.key_claims
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
            evidence_relationships.extend(causal.evidence_mappings)
        if stakeholder:
            evidence_relationships.extend(stakeholder.evidence_mappings)
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
                item
                if isinstance(item, dict)
                else {
                    "factor_id": new_id("structural_factor"),
                    "description": str(item),
                    "evidence_ids": [],
                    "status": "UNVERIFIED_DUE_TO_PARTIAL_FAILURE",
                }
                for item in final.causal_structure.get("structural_factors", [])
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
            problem_definition=final.problem_definition,
            claim_structure=claim_structure,
            key_assumptions=key_assumptions,
            evidence_relationships=evidence_relationships,
            causal_model=final.causal_structure,
            structural_factors=structural_factors,
            stakeholder_structure=final.stakeholder_structure,
            existing_response_evaluation=final.existing_response_assessment,
            counterarguments=[item.model_dump(mode="json") for item in counterargument.counterarguments],
            alternative_interpretations=counterargument.alternative_interpretations,
            trade_offs=final.tradeoffs,
            uncertainties=list(
                dict.fromkeys(final.uncertainties + counterargument.remaining_uncertainties)
            ),
            analysis_perspectives=final.major_viewpoints,
            unresolved_issues=final.unresolved_questions,
            research_gaps=[item.model_dump(mode="json") for item in report.evidence_gaps],
            source_traceability=source_traceability,
            analysis_traceability=analysis_traceability,
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
            stages.extend(["initial_integration", "counterargument", "final_integration", "quality_review"])
        elif COUNTERARGUMENT_ANALYST_ID in targets:
            stages.extend(["counterargument", "final_integration", "quality_review"])
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
