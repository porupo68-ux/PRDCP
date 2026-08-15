from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

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
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from common.validation import PMPValidator
from conclusion.registry import ConclusionRegistry
from conclusion.schemas import (
    DEFAULT_CRITERIA,
    ConclusionPackage,
    ConclusionQualityReviewInput,
    ConclusionQualityReviewOutput,
    DecisionContext,
    DecisionEvaluationResult,
    DecisionEvaluationTask,
    DecisionIntegrationResult,
    DecisionIntegrationTask,
    DeterministicValidationResult,
    EvaluationFramework,
    EvaluationRating,
    FinalConclusion,
    HumanSelection,
    PositionGenerationResult,
    PositionGenerationTask,
    QualityGateDecision,
    RevisionScope,
    SelectionType,
    default_value_profiles,
)
from conclusion.state import (
    ConclusionRevisionRecord,
    ConclusionUpstreamRevisionRecord,
    ConclusionWorkflowState,
    utc_now,
)
from conclusion.validator import ConclusionValidator
from conclusion.workflow import (
    DECISION_EVALUATOR_ID,
    DECISION_INTEGRATOR_ID,
    POSITION_GENERATOR_ID,
    QUALITY_REVIEWER_ID,
)
from deliberation.schemas.deliberation_result import DeliberationResult
from storage.conclusion_workflow_repository import ConclusionWorkflowRepository


ProgressCallback = Callable[[str], Awaitable[None]]


class ConclusionManager:
    agent_id = "conclusion.manager"

    def __init__(
        self,
        registry: ConclusionRegistry,
        repository: ConclusionWorkflowRepository,
        *,
        max_revisions: int = 2,
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.demo_safe_mode = demo_safe_mode
        self.max_revisions = 0 if demo_safe_mode else max_revisions
        self.rd_loader = rd_loader or registry.rd_loader
        self.pmp_validator = PMPValidator()
        self.deterministic_validator = ConclusionValidator()

    async def start(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
        try:
            return self.repository.load(workflow_id)
        except FileNotFoundError:
            pass
        return await self.start_from_message(
            self.repository.load_deliberation_handoff(workflow_id),
            progress_callback=progress_callback,
        )

    async def start_from_message(
        self,
        handoff: PMPMessage,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime.revision_limit is not None:
            self.max_revisions = runtime.revision_limit
        result = self._validate_deliberation_handoff(handoff)
        context = self._build_decision_context(result)
        state = ConclusionWorkflowState(
            workflow_id=handoff.workflow_id,
            deliberation_handoff=handoff.model_dump(mode="json"),
            deliberation_result=result.model_dump(mode="json"),
            decision_context=context.model_dump(mode="json"),
            message_history=[handoff],
            role_definition_usage=[manager_snapshot.trace()],
        )
        self.repository.save(state)
        await self._emit(progress_callback, f"Conclusion Workflow開始: {state.workflow_id}")
        return await self._run_generation_and_review(
            state,
            rerun_position=True,
            rerun_evaluation=True,
            rerun_integration=True,
            progress_callback=progress_callback,
        )

    async def resume(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.WAITING_UPSTREAM_REVISION.value:
            raise ValueError("Conclusion workflow is not waiting for an upstream revision")
        handoff = self.repository.load_deliberation_handoff(workflow_id)
        if handoff.message_id == state.deliberation_handoff.get("message_id"):
            raise ValueError("Deliberationから新しいrevision resultがまだ届いていません")
        result = self._validate_deliberation_handoff(handoff)
        manager_snapshot = self.rd_loader.load(self.agent_id)
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())
        state.deliberation_handoff = handoff.model_dump(mode="json")
        state.deliberation_result = result.model_dump(mode="json")
        state.decision_context = self._build_decision_context(result).model_dump(mode="json")
        state.position_generation = None
        state.position_candidates = []
        state.evaluation_framework = None
        state.decision_evaluation = None
        state.decision_integration = None
        state.conclusion_package = None
        state.deterministic_validation = None
        state.review_result = None
        state.human_selection = None
        state.final_conclusion = None
        state.completed_agents = []
        state.failed_agents = []
        state.current_agent_ids = []
        state.revision_count = 0
        state.error = None
        state.message_history.append(handoff)
        self.repository.save(state)
        await self._emit(progress_callback, "Deliberation修正結果を受領し、Conclusionを再開します")
        return await self._run_generation_and_review(
            state,
            rerun_position=True,
            rerun_evaluation=True,
            rerun_integration=True,
            progress_callback=progress_callback,
        )

    async def integrate_candidates(
        self,
        workflow_id: str,
        candidate_ids: list[str],
        *,
        user_instruction: str | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.WAITING_HUMAN_SELECTION.value:
            raise ValueError("Conclusion is not waiting for human selection")
        if len(candidate_ids) < 2:
            raise ValueError("Integration requires at least two candidates")
        known = {item["position_candidate_id"] for item in state.position_candidates}
        unknown = set(candidate_ids) - known
        if unknown:
            raise ValueError(f"Unknown candidate IDs: {sorted(unknown)}")
        state.review_result = None
        self.repository.save(state)
        return await self._run_generation_and_review(
            state,
            rerun_position=False,
            rerun_evaluation=False,
            rerun_integration=True,
            requested_integration_candidate_ids=candidate_ids,
            user_instruction=user_instruction,
            progress_callback=progress_callback,
        )

    def select(
        self,
        workflow_id: str,
        candidate_ids: list[str],
        *,
        selection_type: str = SelectionType.CANDIDATE.value,
        user_instruction: str | None = None,
        accepted_tradeoffs: list[str] | None = None,
        accepted_limitations: list[str] | None = None,
    ) -> ConclusionWorkflowState:
        state = self.repository.load(workflow_id)
        if state.status == WorkflowStatus.COMPLETED.value and state.final_conclusion:
            return state
        if state.status != WorkflowStatus.WAITING_HUMAN_SELECTION.value:
            raise ValueError("Conclusion is not waiting for human selection")
        review = ConclusionQualityReviewOutput.model_validate(state.review_result)
        if review.status not in {
            QualityGateDecision.APPROVED.value,
            QualityGateDecision.APPROVED_WITH_CONDITIONS.value,
        }:
            raise ValueError("Only a quality-approved Conclusion Package can be selected")
        selection_kind = SelectionType(selection_type)
        if selection_kind == SelectionType.DEFER:
            return state
        known = {item["position_candidate_id"] for item in state.position_candidates}
        unknown = set(candidate_ids) - known
        if unknown:
            raise ValueError(f"Unknown candidate IDs: {sorted(unknown)}")
        if selection_kind == SelectionType.CANDIDATE and len(candidate_ids) != 1:
            raise ValueError("candidate selection requires exactly one candidate ID")
        if selection_kind == SelectionType.INTEGRATED_OPTION and not state.decision_integration.get(
            "integrated_option"
        ):
            raise ValueError("No integrated option is available")

        state.status = WorkflowStatus.FINALIZING
        selection = HumanSelection(
            selection_id=new_id("human_selection"),
            workflow_id=workflow_id,
            selected_candidate_ids=candidate_ids,
            selection_type=selection_kind,
            user_instruction=user_instruction,
            accepted_tradeoffs=accepted_tradeoffs or [],
            accepted_limitations=accepted_limitations or [],
            rejected_candidate_ids=sorted(known - set(candidate_ids)),
        )
        final = self._build_final_conclusion(state, selection)
        state.human_selection = selection.model_dump(mode="json")
        state.final_conclusion = final.model_dump(mode="json")
        self.repository.save_final_conclusion(final)
        try:
            self._send_to_playwright(state, final)
        except Exception as exc:
            state.status = WorkflowStatus.FAILED
            state.error = {"stage": "playwright_handoff", "message": str(exc)}
            self.repository.save(state)
            return state
        state.playwright_sent = True
        state.status = WorkflowStatus.COMPLETED
        state.completed_at = utc_now()
        state.error = None
        self.repository.save(state)
        return state

    async def _run_generation_and_review(
        self,
        state: ConclusionWorkflowState,
        *,
        rerun_position: bool,
        rerun_evaluation: bool,
        rerun_integration: bool,
        requested_integration_candidate_ids: list[str] | None = None,
        user_instruction: str | None = None,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        while True:
            try:
                context = DecisionContext.model_validate(state.decision_context)
                if rerun_position:
                    state.status = WorkflowStatus.GENERATING_POSITIONS
                    generation = await self._generate_positions(state, context)
                    state.position_generation = generation.model_dump(mode="json")
                    state.position_candidates = [
                        item.model_dump(mode="json") for item in generation.position_candidates
                    ]
                    self.repository.save(state)
                    await self._emit(progress_callback, f"Position Generator完了: {len(state.position_candidates)}候補")
                else:
                    generation = PositionGenerationResult.model_validate(state.position_generation)

                framework = self._build_evaluation_framework(context)
                state.evaluation_framework = framework.model_dump(mode="json")
                if rerun_position or rerun_evaluation:
                    state.status = WorkflowStatus.EVALUATING_POSITIONS
                    evaluation = await self._evaluate_positions(state, context, generation, framework)
                    state.decision_evaluation = evaluation.model_dump(mode="json")
                    self.repository.save(state)
                    await self._emit(progress_callback, "Decision Evaluator完了")
                else:
                    evaluation = DecisionEvaluationResult.model_validate(state.decision_evaluation)

                if rerun_position or rerun_evaluation or rerun_integration:
                    state.status = WorkflowStatus.INTEGRATING_DECISION
                    integration = await self._integrate_decision(
                        state,
                        context,
                        generation,
                        evaluation,
                        requested_candidate_ids=requested_integration_candidate_ids or [],
                        user_instruction=user_instruction,
                    )
                    state.decision_integration = integration.model_dump(mode="json")
                    self.repository.save(state)
                    await self._emit(progress_callback, "Decision Integrator完了")
                else:
                    integration = DecisionIntegrationResult.model_validate(state.decision_integration)

                package = self._build_package(state, context, generation, evaluation, integration)
                validation = self.deterministic_validator.validate(
                    decision_context=context,
                    position_generation=generation,
                    decision_evaluation=evaluation,
                    decision_integration=integration,
                    conclusion_package=package,
                    human_selection_present=state.human_selection is not None,
                )
                state.conclusion_package = package.model_dump(mode="json")
                state.deterministic_validation = validation.model_dump(mode="json")
                self.repository.save(state)
                review, response = await self._request_review(
                    state,
                    generation,
                    evaluation,
                    integration,
                    package,
                    validation,
                )
            except Exception as exc:
                return await self._fail(
                    state,
                    f"Conclusion生成またはQuality Reviewに失敗しました: {exc}",
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
                package.quality_review = review.model_dump(mode="json")
                state.conclusion_package = package.model_dump(mode="json")
                state.limitations = list(
                    dict.fromkeys(state.limitations + review.limitations_to_disclose)
                )
                state.status = WorkflowStatus.WAITING_HUMAN_SELECTION
                state.current_agent_ids = []
                self.repository.save_package(package)
                self.repository.save(state)
                await self._emit(progress_callback, "Quality Gate通過。ユーザー選択待ちです")
                return state
            if review.status == QualityGateDecision.BLOCKED.value:
                return await self._block(state, review.reason, progress_callback)
            if self.demo_safe_mode:
                return await self._block(
                    state,
                    "Demo Safe Mode stopped automatic reviewer revision and Manager re-dispatch",
                    progress_callback,
                )
            if review.revision_scope == RevisionScope.DELIBERATION_RETURN.value:
                return await self._request_upstream_revision(
                    state,
                    review,
                    response.message_id,
                    progress_callback,
                )

            state.revision_count += 1
            stages = self._revision_stages(review.revision_targets)
            state.revision_history.append(
                ConclusionRevisionRecord(
                    iteration=state.revision_count,
                    target_agent_ids=review.revision_targets,
                    findings=[item.model_dump(mode="json") for item in review.findings],
                    rerun_stages=stages,
                )
            )
            if state.revision_count >= self.max_revisions:
                return await self._block(
                    state,
                    f"Quality Reviewerが{self.max_revisions}回revision_requiredを返したため停止しました",
                    progress_callback,
                )
            rerun_position = POSITION_GENERATOR_ID in review.revision_targets
            rerun_evaluation = rerun_position or DECISION_EVALUATOR_ID in review.revision_targets
            rerun_integration = (
                rerun_evaluation
                or DECISION_INTEGRATOR_ID in review.revision_targets
                or self.agent_id in review.revision_targets
            )
            if not (rerun_position or rerun_evaluation or rerun_integration):
                return await self._fail(
                    state,
                    "revision_requiredを実行可能な依存関係へ解決できませんでした",
                    progress_callback,
                )
            requested_integration_candidate_ids = []
            user_instruction = None
            await self._emit(
                progress_callback,
                "Quality Reviewer: revision_required → "
                + ", ".join(review.revision_targets)
                + f"（{state.revision_count}/{self.max_revisions}）",
            )

    async def _generate_positions(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
    ) -> PositionGenerationResult:
        task = PositionGenerationTask(
            task_id=new_id("position_task"),
            target_agent_id=POSITION_GENERATOR_ID,
            decision_context=context,
            deliberation_result=state.deliberation_result,
            requested_candidate_count=3,
            revision_context=self._latest_revision_context(state),
        )
        return await self._execute_agent(
            state,
            agent_id=POSITION_GENERATOR_ID,
            message_type=MessageType.POSITION_GENERATION_ASSIGNMENT,
            expected_type=MessageType.POSITION_GENERATION_RESULT,
            objective="Generate substantively distinct, traceable position candidates",
            payload=task.model_dump(mode="json"),
            output_schema=PositionGenerationResult,
            previous_stage="deliberation",
            next_stage="conclusion.decision_evaluator",
        )

    async def _evaluate_positions(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        generation: PositionGenerationResult,
        framework: EvaluationFramework,
    ) -> DecisionEvaluationResult:
        task = DecisionEvaluationTask(
            task_id=new_id("evaluation_task"),
            target_agent_id=DECISION_EVALUATOR_ID,
            decision_context=context,
            position_candidates=generation.position_candidates,
            evaluation_framework=framework,
            revision_context=self._latest_revision_context(state),
        )
        return await self._execute_agent(
            state,
            agent_id=DECISION_EVALUATOR_ID,
            message_type=MessageType.DECISION_EVALUATION_ASSIGNMENT,
            expected_type=MessageType.DECISION_EVALUATION_RESULT,
            objective="Evaluate every candidate under one non-compensatory framework",
            payload=task.model_dump(mode="json"),
            output_schema=DecisionEvaluationResult,
            previous_stage=POSITION_GENERATOR_ID,
            next_stage=DECISION_INTEGRATOR_ID,
        )

    async def _integrate_decision(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        generation: PositionGenerationResult,
        evaluation: DecisionEvaluationResult,
        *,
        requested_candidate_ids: list[str],
        user_instruction: str | None,
    ) -> DecisionIntegrationResult:
        task = DecisionIntegrationTask(
            task_id=new_id("integration_task"),
            target_agent_id=DECISION_INTEGRATOR_ID,
            decision_context=context,
            position_candidates=generation.position_candidates,
            decision_evaluation=evaluation,
            requested_integration_candidate_ids=requested_candidate_ids,
            user_instruction=user_instruction,
            revision_context=self._latest_revision_context(state),
        )
        return await self._execute_agent(
            state,
            agent_id=DECISION_INTEGRATOR_ID,
            message_type=MessageType.DECISION_INTEGRATION_ASSIGNMENT,
            expected_type=MessageType.DECISION_INTEGRATION_RESULT,
            objective="Create a selectable Conclusion Package without replacing human choice",
            payload=task.model_dump(mode="json"),
            output_schema=DecisionIntegrationResult,
            previous_stage=DECISION_EVALUATOR_ID,
            next_stage=QUALITY_REVIEWER_ID,
        )

    async def _request_review(
        self,
        state: ConclusionWorkflowState,
        generation: PositionGenerationResult,
        evaluation: DecisionEvaluationResult,
        integration: DecisionIntegrationResult,
        package: ConclusionPackage,
        validation: DeterministicValidationResult,
    ) -> tuple[ConclusionQualityReviewOutput, PMPMessage]:
        state.status = WorkflowStatus.REVIEWING
        review_input = ConclusionQualityReviewInput(
            position_generation=generation,
            decision_evaluation=evaluation,
            decision_integration=integration,
            conclusion_package=package,
            deterministic_validation=validation,
            revision_context=self._latest_revision_context(state),
        )
        result, response = await self._execute_agent(
            state,
            agent_id=QUALITY_REVIEWER_ID,
            message_type=MessageType.CONCLUSION_QUALITY_REVIEW_ASSIGNMENT,
            expected_type=MessageType.CONCLUSION_QUALITY_REVIEW_RESULT,
            objective="Review Conclusion integrity, traceability, boundaries, and human-selection readiness",
            payload=review_input.model_dump(mode="json"),
            output_schema=ConclusionQualityReviewOutput,
            previous_stage=DECISION_INTEGRATOR_ID,
            next_stage=self.agent_id,
            return_response=True,
        )
        return result, response

    async def _execute_agent(
        self,
        state: ConclusionWorkflowState,
        *,
        agent_id: str,
        message_type: MessageType,
        expected_type: MessageType,
        objective: str,
        payload: dict[str, Any],
        output_schema,
        previous_stage: str,
        next_stage: str,
        return_response: bool = False,
    ):
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=agent_id,
            message_type=message_type,
            objective=objective,
            payload=payload,
            constraints={
                "new_evidence_allowed": False,
                "human_final_selection_allowed": False,
                "route_revisions_only_through_manager": True,
            },
            context=PMPContext(
                current_stage=agent_id,
                previous_stage=previous_stage,
                next_stage=next_stage,
            ),
            routing=PMPRouting(
                revision_target=agent_id if state.revision_count else None,
                reply_required=True,
            ),
            metadata=PMPMetadata(
                status=MessageStatus.REVISION_REQUIRED if state.revision_count else MessageStatus.QUEUED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        state.current_agent_ids = [agent_id]
        state.message_history.append(request)
        self.repository.save(state)
        response = await self.registry.get(agent_id).execute(request)
        state.message_history.append(response)
        state.current_agent_ids = []
        error = self._validate_response_envelope(request, response, agent_id, expected_type.value)
        if error:
            if agent_id not in state.failed_agents:
                state.failed_agents.append(agent_id)
            self.repository.save(state)
            raise ValueError(error)
        if agent_id not in state.completed_agents:
            state.completed_agents.append(agent_id)
        if agent_id in state.failed_agents:
            state.failed_agents.remove(agent_id)
        self.repository.save(state)
        result = output_schema.model_validate(response.payload)
        task_id = payload.get("task_id")
        if task_id and getattr(result, "task_id", task_id) != task_id:
            raise ValueError(f"Task ID mismatch from {agent_id}")
        return (result, response) if return_response else result

    def _validate_deliberation_handoff(self, handoff: PMPMessage) -> DeliberationResult:
        self.pmp_validator.validate(handoff)
        if handoff.sender_agent_id != "deliberation.manager":
            raise ValueError("Conclusion handoff sender must be deliberation.manager")
        if handoff.receiver_agent_id != self.agent_id:
            raise ValueError("Conclusion handoff receiver must be conclusion.manager")
        if handoff.message_type != MessageType.DELIBERATION_RESULT.value:
            raise ValueError("Conclusion handoff must use deliberation_result")
        payload = handoff.payload.get("deliberation_result") or handoff.payload
        allowed = DeliberationResult.model_fields.keys()
        result = DeliberationResult.model_validate({key: payload[key] for key in allowed if key in payload})
        if result.workflow_id != handoff.workflow_id:
            raise ValueError("Deliberation Result workflow_id mismatch")
        review = result.quality_review or handoff.payload.get("quality_review") or {}
        if review.get("status") not in {"approved", "approved_with_conditions"}:
            raise ValueError("Deliberation Result has not passed its Quality Gate")
        if review.get("conclusion_readiness", "READY") not in {"READY", "READY_WITH_CONDITIONS"}:
            raise ValueError("Deliberation Result is not Conclusion-ready")
        if review.get("blocking_finding_ids"):
            raise ValueError("Deliberation Result contains blocking findings")
        if not 1 <= len(result.analysis_perspectives) <= 3:
            raise ValueError("Deliberation Result must contain one to three viewpoints")
        claim_ids = [str(item.get("claim_id")) for item in result.claim_structure]
        if not all(item and item != "None" for item in claim_ids) or len(claim_ids) != len(set(claim_ids)):
            raise ValueError("Deliberation claim IDs must be present and unique")
        evidence_ids = {str(item.get("evidence_id")) for item in result.source_traceability}
        source_ids = {str(item.get("source_id")) for item in result.source_traceability}
        if not evidence_ids or "None" in evidence_ids or not source_ids or "None" in source_ids:
            raise ValueError("Deliberation traceability must include evidence_id and source_id")
        if result.claim_traceability:
            known_analysis_ids = {
                str(item.get("analysis_id"))
                for item in result.analysis_traceability
                if item.get("analysis_id")
            }
            source_by_evidence = {
                str(item.get("evidence_id")): str(item.get("source_id"))
                for item in result.source_traceability
                if item.get("evidence_id") and item.get("source_id")
            }
            traced_claim_ids: set[str] = set()
            for entry in result.claim_traceability:
                if not entry.claim_ids:
                    continue
                if not entry.analysis_ids or not entry.evidence_ids or not entry.source_ids:
                    raise ValueError(
                        "Claim traceability requires analysis, evidence, and source IDs"
                    )
                if set(entry.analysis_ids) - known_analysis_ids:
                    raise ValueError("Claim traceability references unknown analysis IDs")
                if set(entry.evidence_ids) - set(source_by_evidence):
                    raise ValueError("Claim traceability references unknown evidence IDs")
                expected_sources = {
                    source_by_evidence[evidence_id]
                    for evidence_id in entry.evidence_ids
                }
                if expected_sources - set(entry.source_ids):
                    raise ValueError(
                        "Claim traceability cannot complete evidence to source traversal"
                    )
                traced_claim_ids.update(entry.claim_ids)
            if set(claim_ids) - traced_claim_ids:
                raise ValueError(
                    "Every Deliberation claim needs claim -> analysis -> evidence -> source traceability"
                )
        return result

    def _build_decision_context(self, result: DeliberationResult) -> DecisionContext:
        problem = result.problem_definition
        decision_question = str(
            problem.get("decision_question")
            or problem.get("definition")
            or f"{result.topic}について、どの立場と提言を採用すべきか"
        )
        stakeholder_ids = self._collect_ids(result.stakeholder_structure, "stakeholder_id")
        affected = self._collect_dicts_with_key(result.stakeholder_structure, "stakeholder_id")
        if not affected:
            names = result.stakeholder_structure.get("primary", [])
            affected = [
                {"stakeholder_id": f"stakeholder_{index}", "name": str(name)}
                for index, name in enumerate(names, start=1)
            ]
            stakeholder_ids.update(item["stakeholder_id"] for item in affected)
        claims = [str(item["claim_id"]) for item in result.claim_structure]
        evidence_ids = sorted({str(item["evidence_id"]) for item in result.source_traceability})
        source_ids = sorted({str(item["source_id"]) for item in result.source_traceability})
        analysis_ids = sorted(
            str(item["analysis_id"])
            for item in result.analysis_traceability
            if item.get("analysis_id")
        )
        return DecisionContext(
            decision_context_id=new_id("decision_context"),
            workflow_id=result.workflow_id,
            deliberation_result_id=result.deliberation_result_id,
            decision_question=decision_question,
            target_problem={**problem, "problem_id": str(problem.get("problem_id") or result.deliberation_result_id)},
            goals=["対象問題を軽減する", "追跡可能で実施条件の明確な選択肢を提示する"],
            non_goals=["新規調査", "台本作成", "ユーザーの最終選択の代行"],
            constraints=list(dict.fromkeys(result.limitations)),
            non_negotiable_constraints=["Evidence Traceabilityを保持する", "Blocking Issueを相殺しない"],
            affected_stakeholders=affected,
            major_viewpoints=[item.model_dump(mode="json") for item in result.analysis_perspectives],
            key_claim_ids=claims,
            evidence_ids=evidence_ids,
            analysis_ids=analysis_ids,
            source_ids=source_ids,
            tradeoffs=result.trade_offs,
            uncertainties=result.uncertainties,
            limitations=result.limitations,
            evaluation_criteria=DEFAULT_CRITERIA,
            value_profiles=default_value_profiles(),
        )

    def _build_evaluation_framework(self, context: DecisionContext) -> EvaluationFramework:
        problem = context.target_problem
        stakeholder_names = [str(item.get("name") or item.get("stakeholder_id")) for item in context.affected_stakeholders]
        return EvaluationFramework(
            evaluation_framework_id=new_id("evaluation_framework"),
            criteria=DEFAULT_CRITERIA,
            rating_scale=[item.value for item in EvaluationRating],
            value_profiles=default_value_profiles(),
            common_time_scope=str(problem.get("time_scope") or "Deliberation-defined time scope"),
            common_geographic_scope=str(problem.get("geographic_scope") or "Deliberation-defined geographic scope"),
            common_target_population=", ".join(stakeholder_names) or "Deliberation-defined stakeholders",
        )

    def _build_package(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        generation: PositionGenerationResult,
        evaluation: DecisionEvaluationResult,
        integration: DecisionIntegrationResult,
    ) -> ConclusionPackage:
        result = DeliberationResult.model_validate(state.deliberation_result)
        recommended = integration.recommended_options[0] if integration.recommended_options else None
        return ConclusionPackage(
            conclusion_package_id=(
                state.conclusion_package.get("conclusion_package_id")
                if state.conclusion_package
                else new_id("conclusion_package")
            ),
            workflow_id=state.workflow_id,
            topic=result.topic,
            general_opinion=result.general_opinion,
            decision_question=context.decision_question,
            problem_summary=self._summary(result.problem_definition),
            deliberation_summary=" / ".join(item.title for item in result.analysis_perspectives),
            options=[item.model_dump(mode="json") for item in generation.position_candidates],
            comparison_matrix=[
                item.model_dump(mode="json") for item in evaluation.comparison_matrix
            ],
            primary_recommendation=(
                recommended.model_dump(mode="json") if recommended else None
            ),
            alternatives=[
                item.model_dump(mode="json")
                for item in integration.recommended_options[1:]
            ],
            integrated_option=(
                integration.integrated_option.model_dump(mode="json")
                if integration.integrated_option
                else None
            ),
            key_tradeoffs=[
                item.model_dump(mode="json") for item in integration.major_tradeoffs
            ],
            unresolved_value_conflicts=[
                item.model_dump(mode="json")
                for item in integration.unresolved_value_conflicts
            ],
            uncertainties=list(dict.fromkeys(context.uncertainties + integration.accepted_uncertainties)),
            limitations=list(dict.fromkeys(context.limitations + integration.limitations)),
            evidence_traceability=result.source_traceability,
            analysis_traceability=result.analysis_traceability,
            selection_required=True,
            quality_review=None,
        )

    def _build_final_conclusion(
        self,
        state: ConclusionWorkflowState,
        selection: HumanSelection,
    ) -> FinalConclusion:
        package = ConclusionPackage.model_validate(state.conclusion_package)
        selected_candidates = [
            item for item in state.position_candidates
            if item["position_candidate_id"] in set(selection.selected_candidate_ids)
        ]
        if selection.selection_type == SelectionType.INTEGRATED_OPTION.value:
            selected = dict(state.decision_integration["integrated_option"])
            sources = selected_candidates or state.position_candidates
        else:
            selected = dict(selected_candidates[0])
            sources = selected_candidates
        recommendation = str(
            selected.get("summary")
            or selected.get("recommendation")
            or selection.user_instruction
            or package.primary_recommendation
        )
        implementation = self._merge_lists(sources, "implementation_steps")
        if not implementation:
            implementation = [str(selected.get("implementation_direction") or "選択案の実施条件を満たして実行する")]
        claims = self._merge_lists(sources, "supporting_claim_ids")
        evidence = self._merge_lists(sources, "supporting_evidence_ids")
        analyses = self._merge_lists(sources, "supporting_analysis_ids")
        source_map = {
            str(item["evidence_id"]): str(item["source_id"])
            for item in package.evidence_traceability
        }
        source_ids = list(dict.fromkeys(source_map[item] for item in evidence if item in source_map))
        if not (claims and evidence and analyses and source_ids):
            raise ValueError("Selected option does not preserve complete traceability")
        return FinalConclusion(
            final_conclusion_id=new_id("final_conclusion"),
            workflow_id=state.workflow_id,
            conclusion_package_id=package.conclusion_package_id,
            human_selection_id=selection.selection_id,
            selected_position=selected,
            final_recommendation=recommendation,
            implementation_direction=implementation,
            responsible_actors=self._merge_lists(sources, "responsible_actors") or ["選択案で指定された実施主体"],
            expected_benefits=self._merge_lists(sources, "expected_benefits"),
            accepted_tradeoffs=selection.accepted_tradeoffs or self._merge_lists(sources, "tradeoffs"),
            accepted_risks=self._merge_lists(sources, "risks"),
            uncertainties=package.uncertainties,
            limitations=list(dict.fromkeys(package.limitations + selection.accepted_limitations)),
            supporting_claim_ids=claims,
            supporting_analysis_ids=analyses,
            supporting_evidence_ids=evidence,
            supporting_source_ids=source_ids,
            rejected_alternatives_summary=[
                {"candidate_id": item["position_candidate_id"], "title": item["title"], "reason": "ユーザーの最終選択では不採用"}
                for item in state.position_candidates
                if item["position_candidate_id"] in selection.rejected_candidate_ids
            ],
        )

    def _send_to_playwright(self, state: ConclusionWorkflowState, final: FinalConclusion) -> None:
        result = DeliberationResult.model_validate(state.deliberation_result)
        package = ConclusionPackage.model_validate(state.conclusion_package)
        selection = HumanSelection.model_validate(state.human_selection)
        final_payload = final.model_dump(mode="json")
        package_payload = package.model_dump(mode="json")
        selection_payload = selection.model_dump(mode="json")
        traceability_manifest = {
            "claim_ids": final.supporting_claim_ids,
            "analysis_ids": final.supporting_analysis_ids,
            "evidence_ids": final.supporting_evidence_ids,
            "source_ids": final.supporting_source_ids,
            "sources": [
                item
                for item in result.source_traceability
                if item.get("evidence_id") in final.supporting_evidence_ids
            ],
        }
        payload = {
            "final_conclusion": final_payload,
            "conclusion_package": package_payload,
            "human_selection": selection_payload,
            "traceability_manifest": traceability_manifest,
            "limitations_to_disclose": list(dict.fromkeys(state.limitations + final.limitations)),
            "conclusion_id": final.final_conclusion_id,
            "topic": package.topic,
            "general_opinion": package.general_opinion,
            "central_question": package.decision_question,
            "selected_position": final.selected_position,
            "recommendations": final.implementation_direction,
            "decision_rationale": final.final_recommendation,
            "supporting_claims": [item for item in result.claim_structure if item.get("claim_id") in final.supporting_claim_ids],
            "supporting_analysis": [item for item in result.analysis_traceability if item.get("analysis_id") in final.supporting_analysis_ids],
            "evidence_links": [item for item in result.source_traceability if item.get("evidence_id") in final.supporting_evidence_ids],
            "evaluation_summary": state.decision_evaluation,
            "implementation_conditions": final.selected_position.get("success_conditions", []),
            "expected_benefits": final.expected_benefits,
            "risks": final.accepted_risks,
            "trade_offs": final.accepted_tradeoffs,
            "affected_stakeholders": DecisionContext.model_validate(state.decision_context).affected_stakeholders,
            "counterarguments": result.counterarguments,
            "uncertainties": final.uncertainties,
            "limitations": final.limitations,
            "unresolved_issues": result.unresolved_issues,
            "prohibited_interpretations": ["不確実性を確定事項として表現しない", "反対Evidenceを省略しない"],
            "source_registry_reference": result.source_traceability,
            "quality_review": state.review_result,
            "workflow_metadata": {
                "workflow_id": state.workflow_id,
                "conclusion_package_id": package.conclusion_package_id,
                "human_selection": selection_payload,
                "role_definition_usage": state.role_definition_usage,
            },
        }
        self._validate_playwright_handoff(payload)
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=state.message_history[-1].message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="playwright.manager",
            message_type=MessageType.CONCLUSION_HANDOFF,
            objective="Produce a script from the human-selected final conclusion",
            payload=payload,
            constraints={"preserve_human_selection": True, "content_decision_changes_allowed": False},
            context=PMPContext(
                current_stage="conclusion.finalized",
                previous_stage="conclusion.human_selection",
                next_stage="playwright",
            ),
            metadata=PMPMetadata(
                status=MessageStatus.COMPLETED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        self.pmp_validator.validate(message)
        self.repository.save_playwright_outbox(message)
        state.message_history.append(message)

    async def _request_upstream_revision(
        self,
        state: ConclusionWorkflowState,
        review: ConclusionQualityReviewOutput,
        parent_message_id: str,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        requests = [item.model_dump(mode="json") for item in review.upstream_revision_requests]
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=parent_message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="deliberation.manager",
            message_type=MessageType.REVISION_REQUEST,
            objective="Revise Deliberation analysis required for a valid Conclusion",
            payload={
                "deliberation_result_id": state.deliberation_result["deliberation_result_id"],
                "revision_requests": requests,
                "quality_review_id": review.review_id,
            },
            constraints={"new_evidence_only_if_routed_to_researcher": True, "preserve_traceability": True},
            context=PMPContext(
                current_stage="conclusion.upstream_revision",
                previous_stage="conclusion.quality_review",
                next_stage="deliberation",
            ),
            routing=PMPRouting(revision_target="deliberation.manager", reply_required=True),
            metadata=PMPMetadata(
                status=MessageStatus.REVISION_REQUIRED,
                extensions={"role_definition": state.role_definition_usage[-1]},
            ),
        )
        self.pmp_validator.validate(message)
        self.repository.save_deliberation_revision_outbox(message)
        state.message_history.append(message)
        state.upstream_revision_count += 1
        state.upstream_revision_history.append(
            ConclusionUpstreamRevisionRecord(
                iteration=state.upstream_revision_count,
                request_message_id=message.message_id,
                requests=requests,
            )
        )
        state.status = WorkflowStatus.WAITING_UPSTREAM_REVISION
        state.error = None
        self.repository.save(state)
        await self._emit(progress_callback, "Deliberationへ分析修正を要求し、Workflowを待機状態にしました")
        return state

    def _validate_response_envelope(
        self,
        request: PMPMessage,
        response: PMPMessage,
        sender_agent_id: str,
        expected_type: str,
    ) -> str | None:
        try:
            self.pmp_validator.validate(response)
        except Exception as exc:
            return f"Invalid PMP response: {exc}"
        checks = [
            (response.workflow_id == request.workflow_id, "workflow_id mismatch"),
            (response.parent_message_id == request.message_id, "parent_message_id mismatch"),
            (response.sender_agent_id == sender_agent_id, "sender_agent_id mismatch"),
            (response.receiver_agent_id == self.agent_id, "receiver_agent_id mismatch"),
        ]
        for passed, message in checks:
            if not passed:
                return message
        if response.message_type == MessageType.ERROR.value:
            return str(response.payload.get("message") or "Agent returned an error")
        if response.message_type != expected_type:
            return f"Unexpected message_type: {response.message_type}"
        return None

    @staticmethod
    def _validate_playwright_handoff(payload: dict[str, Any]) -> None:
        required = {
            "final_conclusion", "conclusion_package", "human_selection", "traceability_manifest",
            "limitations_to_disclose",
            "conclusion_id", "topic", "general_opinion", "central_question", "selected_position",
            "recommendations", "decision_rationale", "supporting_claims", "supporting_analysis",
            "evidence_links", "evaluation_summary", "implementation_conditions", "expected_benefits",
            "risks", "trade_offs", "affected_stakeholders", "counterarguments", "uncertainties",
            "limitations", "unresolved_issues", "prohibited_interpretations", "source_registry_reference",
            "quality_review", "workflow_metadata",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(f"Conclusion→Playwright handoff is missing: {', '.join(missing)}")
        if not payload["supporting_claims"] or not payload["supporting_analysis"] or not payload["evidence_links"]:
            raise ValueError("Playwright handoff must preserve claim, analysis, and evidence traceability")
        for item in payload["evidence_links"]:
            if not item.get("evidence_id") or not item.get("source_id"):
                raise ValueError("evidence_links require evidence_id and source_id")
        final = payload["final_conclusion"]
        selection = payload["human_selection"]
        package = payload["conclusion_package"]
        trace = payload["traceability_manifest"]
        if final.get("final_conclusion_id") != payload["conclusion_id"]:
            raise ValueError("Final Conclusion ID does not match the canonical handoff ID")
        if final.get("human_selection_id") != selection.get("selection_id"):
            raise ValueError("Human Selection ID does not match Final Conclusion")
        if final.get("conclusion_package_id") != package.get("conclusion_package_id"):
            raise ValueError("Conclusion Package ID does not match Final Conclusion")
        for field, final_key in (
            ("claim_ids", "supporting_claim_ids"),
            ("analysis_ids", "supporting_analysis_ids"),
            ("evidence_ids", "supporting_evidence_ids"),
            ("source_ids", "supporting_source_ids"),
        ):
            if set(final.get(final_key, [])) - set(trace.get(field, [])):
                raise ValueError(f"Traceability Manifest is missing {field}")

    @staticmethod
    def _revision_stages(targets: list[str]) -> list[str]:
        if POSITION_GENERATOR_ID in targets:
            return ["position_generation", "decision_evaluation", "decision_integration", "quality_review"]
        if DECISION_EVALUATOR_ID in targets:
            return ["decision_evaluation", "decision_integration", "quality_review"]
        return ["decision_integration", "quality_review"]

    @staticmethod
    def _latest_revision_context(state: ConclusionWorkflowState) -> dict[str, Any] | None:
        if not state.revision_history:
            return None
        return state.revision_history[-1].model_dump(mode="json")

    @classmethod
    def _collect_ids(cls, value: Any, key: str) -> set[str]:
        ids: set[str] = set()
        if isinstance(value, dict):
            for current, child in value.items():
                if current == key and isinstance(child, str):
                    ids.add(child)
                ids.update(cls._collect_ids(child, key))
        elif isinstance(value, list):
            for child in value:
                ids.update(cls._collect_ids(child, key))
        return ids

    @classmethod
    def _collect_dicts_with_key(cls, value: Any, key: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if isinstance(value, dict):
            if key in value:
                result.append(value)
            for child in value.values():
                result.extend(cls._collect_dicts_with_key(child, key))
        elif isinstance(value, list):
            for child in value:
                result.extend(cls._collect_dicts_with_key(child, key))
        return result

    @staticmethod
    def _merge_lists(records: list[dict[str, Any]], key: str) -> list[str]:
        return list(
            dict.fromkeys(
                str(item)
                for record in records
                for item in record.get(key, [])
                if item is not None and str(item)
            )
        )

    @staticmethod
    def _summary(value: dict[str, Any]) -> str:
        for key in ("summary", "definition", "description", "topic"):
            if value.get(key):
                return str(value[key])
        return str(value)

    async def _block(
        self,
        state: ConclusionWorkflowState,
        message: str,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        state.status = WorkflowStatus.BLOCKED
        state.current_agent_ids = []
        state.error = {"stage": "conclusion", "message": message}
        self.repository.save(state)
        await self._emit(progress_callback, f"Conclusion停止: {message}")
        return state

    async def _fail(
        self,
        state: ConclusionWorkflowState,
        message: str,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        state.status = WorkflowStatus.FAILED
        state.current_agent_ids = []
        state.error = {"stage": "conclusion", "message": message}
        self.repository.save(state)
        await self._emit(progress_callback, f"Conclusion失敗: {message}")
        return state

    @staticmethod
    async def _emit(callback: ProgressCallback | None, message: str) -> None:
        if callback is not None:
            await callback(message)
