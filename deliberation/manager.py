from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

from common.ids import new_id
from common.models.errors import PayloadValidationError, RetryableAgentError
from common.models.pmp import (
    MessageStatus,
    MessageType,
    PMPContext,
    PMPMessage,
    PMPMetadata,
    PMPRouting,
)
from common.models.workflow import WorkflowStatus
from common.models.revision import (
    HumanSelectionImpact,
    LayerId,
    RevisionArtifactRef,
    RevisionAuditEvent,
    RevisionAuditEventType,
    RevisionBudgetPolicy,
    RevisionControlPhase,
    RevisionControlState,
    RevisionExecutionAuthorization,
    RevisionExecutionStatus,
    RevisionFindingDisposition,
    RevisionFindingOutcome,
    RevisionRequestV1,
    RevisionResultV1,
    RevisionRoute,
    canonical_sha256,
    deterministic_revision_request_id,
)
from common.provider_retry import (
    ProviderRetryAuthorization,
    ProviderRetryAuthorizationStore,
    ProviderRetryStatus,
)
from common.provider_contract_repair import (
    PROVIDER_CONTRACT_REPAIR_SUFFIX,
    ProviderContractRepairAuthorization,
    ProviderContractRepairAuthorizationStore,
    ProviderContractRepairStatus,
)
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
from deliberation.schemas.causal_structural_analysis import (
    CausalStructuralAnalysisResult,
    canonicalize_legacy_causal_item_ids,
    canonicalize_legacy_causal_references,
)
from deliberation.schemas.counterargument_analysis import (
    ALTERNATIVE_INTERPRETATION_PREFIX,
    CounterargumentAnalysisResult,
    normalize_saved_counterargument_payload,
)
from deliberation.schemas.deliberation_result import DeliberationResult
from deliberation.schemas.integrated_analysis import (
    FINAL_CAUSAL_TRACEABILITY_PREFIXES,
    FinalIntegratedAnalysis,
    InitialIntegratedAnalysis,
    integration_provenance_errors,
)
from deliberation.schemas.review import (
    ConclusionReadiness,
    DeliberationQualityReviewInput,
    DeliberationQualityReviewOutput,
    DeterministicValidationResult,
    QualityFinding,
    QualityGateDecision,
    RevisionScope,
)
from deliberation.schemas.research_context import (
    DeliberationResearchContext,
    build_deliberation_research_context,
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
from researcher.schemas.human_evidence import (
    AcceptedEvidenceGap,
    HumanEvidenceDecision,
    HumanEvidenceDecisionType,
    HumanEvidenceIntegrityRepair,
    validate_human_evidence_integrity_repair,
)
from researcher.schemas.trace_ids import canonicalize_legacy_trace_ids
from storage.deliberation_workflow_repository import DeliberationWorkflowRepository
from storage.revision_exchange_repository import (
    RevisionBudgetExhausted,
    RevisionExchangeRepository,
)


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
        if (
            getattr(self.registry.provider, "reservation_root", None) is None
            and not os.getenv("PRDCP_DATA_DIR", "").strip()
        ):
            self.registry.provider.reservation_root = (
                repository.data_dir / "provider_call_reservations"
            )
        self.demo_safe_mode = demo_safe_mode
        self.max_revisions = max_revisions
        self.pmp_validator = PMPValidator()
        self.deterministic_validator = DeliberationValidator()
        self.rd_loader = rd_loader or registry.rd_loader
        self.provider_retry_store = ProviderRetryAuthorizationStore(repository.data_dir)
        self.provider_contract_repair_store = (
            ProviderContractRepairAuthorizationStore(repository.data_dir)
        )
        self.revision_exchange = RevisionExchangeRepository(repository.data_dir)

    def authorize_provider_retry(
        self,
        workflow_id: str,
    ) -> ProviderRetryAuthorization:
        """Authorize exactly one retry of the current ambiguous provider task."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Operator provider retry is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError(
                "Deliberation must be FAILED before an operator provider retry"
            )

        if self._preserve_legacy_manager_transport_failure(state):
            self.repository.save(state)
        manager_failure = self._current_manager_provider_failure(state)
        if manager_failure is not None:
            provider_id = getattr(self.registry.provider, "provider_id", None)
            if not isinstance(provider_id, str):
                raise ValueError(
                    "Deliberation Manager provider has no stable logical provider ID"
                )
            return self.provider_retry_store.authorize_once(
                workflow_id=workflow_id,
                provider_id=provider_id,
                agent_id=self.agent_id,
                original_task_id=manager_failure["logical_task_id"],
                source_error_message_id=manager_failure["failure_id"],
                source_error_class=manager_failure["error_class"],
            )

        error_response = self._latest_retryable_review_error(state)
        if error_response is None:
            raise ValueError(
                "No retryable Deliberation Manager or Quality Reviewer failure was found"
            )
        original_task_id = error_response.payload.get("task_id")
        request = next(
            (
                message
                for message in reversed(state.message_history)
                if message.message_id == error_response.parent_message_id
            ),
            None,
        )
        if (
            not isinstance(original_task_id, str)
            or not original_task_id
            or request is None
            or request.sender_agent_id != self.agent_id
            or request.receiver_agent_id != QUALITY_REVIEWER_ID
            or request.message_type
            != MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            or request.payload.get("task_id") != original_task_id
        ):
            raise ValueError(
                "Quality Reviewer error is not correlated to its saved request"
            )
        reviewer = self.registry.get(QUALITY_REVIEWER_ID)
        provider_id = getattr(reviewer.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError(
                "Deliberation Quality Reviewer provider has no stable logical provider ID"
            )
        return self.provider_retry_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=QUALITY_REVIEWER_ID,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            source_error_class=str(error_response.payload.get("error_class") or ""),
        )

    async def retry_provider_call(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        authorization = self.authorize_provider_retry(workflow_id)
        await self._emit(
            progress_callback,
            "Operator one-time provider retry authorized: "
            + authorization.retry_task_id,
        )
        return await self.recover(
            workflow_id,
            progress_callback=progress_callback,
        )

    def authorize_provider_contract_repair(
        self,
        workflow_id: str,
        *,
        repair_model_id: str,
    ) -> ProviderContractRepairAuthorization:
        if not self.demo_safe_mode:
            raise ValueError(
                "Provider contract repair is available only in Demo Safe Mode"
            )
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError("Deliberation must be FAILED before contract repair")
        failure = self._current_manager_provider_failure(state)
        if failure is None or failure.get("error_class") != "ProviderResponseContractError":
            raise ValueError("No contract-invalid Deliberation Manager task was found")
        provider_id = getattr(self.registry.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Deliberation Manager provider has no stable provider ID")
        original_task_id = str(failure["logical_task_id"])
        existing_repair = self.provider_contract_repair_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if existing_repair is not None:
            if (
                existing_repair.status
                == ProviderContractRepairStatus.PENDING.value
                and existing_repair.repair_model_id == repair_model_id.strip()
            ):
                return existing_repair
            raise ValueError(
                "Existing Deliberation Manager contract repair is not reusable"
            )
        retry = self.provider_retry_store.for_original_task(
            workflow_id=workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if retry is None or retry.status != ProviderRetryStatus.CONSUMED.value:
            raise ValueError("Contract repair requires a consumed operator retry")
        retry_reservation_path = self.provider_retry_store.reservation_path(
            provider_id=provider_id,
            workflow_id=workflow_id,
            task_id=retry.retry_task_id,
        )
        retry_reservation = json.loads(
            retry_reservation_path.read_text(encoding="utf-8")
        )
        retry_model_id = str(retry_reservation.get("model_id") or "")
        failure_message = str((state.error or {}).get("message") or "")
        if not retry_model_id or not (
            "OpenRouter HTTP 400" in failure_message
            or "strict JSON contract" in failure_message
        ):
            raise ValueError("Consumed retry has no auditable contract failure")
        source_id = "manager_retry_failure_" + hashlib.sha256(
            failure_message.encode("utf-8")
        ).hexdigest()[:24]
        return self.provider_contract_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=self.agent_id,
            original_task_id=original_task_id,
            retry_task_id=retry.retry_task_id,
            source_error_message_id=source_id,
            source_error_class="NonRetryableAgentError",
            failed_model_id=str(failure.get("model_id") or ""),
            retry_failed_model_id=retry_model_id,
            repair_model_id=repair_model_id.strip(),
        )

    async def repair_provider_contract(
        self,
        workflow_id: str,
        *,
        repair_model_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        authorization = self.authorize_provider_contract_repair(
            workflow_id,
            repair_model_id=repair_model_id,
        )
        await self._emit(
            progress_callback,
            "One-time Deliberation Manager contract repair authorized: "
            + authorization.repair_task_id
            + " -> "
            + authorization.repair_model_id,
        )
        return await self.recover(workflow_id, progress_callback=progress_callback)

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
        canonical_request_message: PMPMessage | None = None
        canonical_result_message: PMPMessage | None = None
        canonical_result: RevisionResultV1 | None = None
        if (
            state.revision_control.phase
            == RevisionControlPhase.WAITING_UPSTREAM_RESULT.value
        ):
            canonical_request_message = self._active_revision_request_message(state)
            canonical_request = RevisionRequestV1.model_validate(
                canonical_request_message.payload
            )
            self.revision_exchange.validator.validate_current_base_artifacts(
                canonical_request,
                {
                    (
                        "researcher.research_report",
                        str(state.research_report.get("research_report_id") or ""),
                    ): canonical_sha256(state.research_report),
                    (
                        "deliberation.final_integration",
                        str((state.final_integration or {}).get("integration_id") or ""),
                    ): canonical_sha256(state.final_integration),
                },
            )
            try:
                canonical_result_message = self.revision_exchange.load_result(
                    requester_layer=LayerId.DELIBERATION,
                    workflow_id=state.workflow_id,
                    revision_request_id=canonical_request.revision_request_id,
                    request_message=canonical_request_message,
                )
            except FileNotFoundError:
                canonical_result_message = None
            if canonical_result_message is not None:
                canonical_result = RevisionResultV1.model_validate(
                    canonical_result_message.payload
                )
        manager_snapshot = self.rd_loader.load(self.agent_id)
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())

        handoff = self.repository.load_researcher_handoff(workflow_id)
        previous_message_id = state.researcher_handoff.get("message_id")
        if handoff.message_id == previous_message_id:
            raise ValueError("Researcherから新しいrevision resultがまだ届いていません")
        report = self._validate_researcher_handoff(handoff, allow_revision=True)
        if canonical_request_message is not None and canonical_result is None:
            canonical_result_message = (
                self._reconstruct_canonical_researcher_result_from_legacy(
                    state,
                    canonical_request_message,
                    handoff,
                    report,
                )
            )
            canonical_result = RevisionResultV1.model_validate(
                canonical_result_message.payload
            )
        if canonical_result is not None:
            report_artifact = next(
                (
                    item
                    for item in canonical_result.result_artifacts
                    if item.artifact_type == "researcher.research_report"
                ),
                None,
            )
            report_payload = report.model_dump(mode="json")
            if (
                report_artifact is None
                or report_artifact.artifact_id != report.research_report_id
                or report_artifact.sha256 != canonical_sha256(report_payload)
            ):
                raise ValueError(
                    "Researcher Revision Result does not match the returned Research Report"
                )
            decision = handoff.payload.get("human_evidence_decision") or {}
            if (
                canonical_result.status
                != RevisionExecutionStatus.COMPLETED.value
                and decision.get("decision")
                != HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS.value
            ):
                raise ValueError(
                    "Incomplete Researcher Revision requires an explicit Human limitations decision"
                )
        tasks = self._create_analysis_tasks(report)
        pending_review = self._pending_revision_review(state)
        pending_targets = list(state.pending_revision_targets)
        stale_primary_targets = self._stale_primary_targets(state, report)
        state.researcher_handoff = handoff.model_dump(mode="json")
        state.research_report = report.model_dump(mode="json")
        state.analysis_tasks = [task.model_dump(mode="json") for task in tasks]
        state.deliberation_result = None
        state.failed_agents = []
        state.current_agent_ids = []
        state.error = None
        state.completed_at = None
        state.status = WorkflowStatus.RUNNING
        for message in (canonical_result_message, handoff):
            if message is not None and not any(
                item.message_id == message.message_id
                for item in state.message_history
            ):
                state.message_history.append(message)
        await self._emit(progress_callback, "Researcher追加調査結果を受領し、Deliberationを再開します")

        # Upstream-only revisions still rerun Manager integration so that the
        # updated report reaches downstream checkpoints without rerunning every
        # primary analyst.
        effective_targets = list(
            dict.fromkeys(
                [*(pending_targets or [self.agent_id]), *stale_primary_targets]
            )
        )
        if canonical_result is not None and canonical_request_message is not None:
            canonical_request = RevisionRequestV1.model_validate(
                canonical_request_message.payload
            )
            state.revision_control = RevisionControlState.model_validate(
                {
                    **state.revision_control.model_dump(mode="json"),
                    "phase": RevisionControlPhase.COMPLETED.value,
                    "active_result_id": canonical_result.revision_result_id,
                    "pending_request_ids": [
                        item
                        for item in state.revision_control.pending_request_ids
                        if item != canonical_request.revision_request_id
                    ],
                    "consumed_request_ids": list(
                        dict.fromkeys(
                            [
                                *state.revision_control.consumed_request_ids,
                                canonical_request.revision_request_id,
                            ]
                        )
                    ),
                    "consumed_result_ids": list(
                        dict.fromkeys(
                            [
                                *state.revision_control.consumed_result_ids,
                                canonical_result.revision_result_id,
                            ]
                        )
                    ),
                }
            )
            self._record_revision_audit(
                state,
                RevisionAuditEvent(
                    audit_event_id=f"result_consumed_{canonical_request.revision_epoch}",
                    workflow_id=state.workflow_id,
                    revision_request_id=canonical_request.revision_request_id,
                    layer=LayerId.DELIBERATION,
                    event_type=RevisionAuditEventType.RESULT_CONSUMED,
                    actor_id=self.agent_id,
                    message_id=canonical_result_message.message_id,
                    artifact_ids=[report.research_report_id],
                    reason="Researcher result and returned handoff were correlated",
                    created_at=canonical_result.completed_at,
                ),
            )
            review_response_id = self._saved_review_response_id(
                state,
                pending_review.review_id,
            )
            request = self._plan_internal_revision(
                state,
                pending_review,
                review_response_id=review_response_id,
                targets=effective_targets,
            )
            state.awaiting_upstream_revision = False
            state.pending_upstream_revision_request_ids = []
            self.repository.save(state)
            if self.demo_safe_mode:
                return await self._block(
                    state,
                    "Demo Safe Mode stopped pending Deliberation revision after Researcher return",
                    progress_callback,
                )
            authorization = self._create_revision_authorization(
                state,
                request,
                actor_id=self.agent_id,
                actor_source="SYSTEM",
                reason="Safe Mode is disabled; continue after correlated Researcher return",
            )
            outcome, rerun_initial, rerun_counterargument = (
                await self._execute_authorized_internal_revision(
                    state,
                    request=request,
                    authorization=authorization,
                    progress_callback=progress_callback,
                )
            )
        else:
            if self.demo_safe_mode:
                return await self._block(
                    state,
                    "Demo Safe Mode stopped pending Deliberation revision after Researcher return",
                    progress_callback,
                )
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

    def _reconstruct_canonical_researcher_result_from_legacy(
        self,
        state: DeliberationWorkflowState,
        request_message: PMPMessage,
        handoff: PMPMessage,
        report: ResearchReport,
    ) -> PMPMessage:
        """Read-compatible 0-call adapter for pre-canonical Researcher replies."""

        if handoff.message_type != MessageType.RESEARCH_REVISION_RESULT.value:
            raise FileNotFoundError(
                "Canonical Researcher Revision Result is missing and no legacy reply is available"
            )
        request = RevisionRequestV1.model_validate(request_message.payload)
        report_payload = report.model_dump(mode="json")
        artifact = RevisionArtifactRef(
            artifact_type="researcher.research_report",
            artifact_id=report.research_report_id,
            sha256=canonical_sha256(report_payload),
        )
        result_id = "revision_result_" + uuid5(
            NAMESPACE_URL, f"{request.revision_request_id}:result"
        ).hex
        result = RevisionResultV1.create(
            revision_result_id=result_id,
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=state.workflow_id,
            requester_layer=LayerId.DELIBERATION,
            producer_layer=LayerId.RESEARCHER,
            revision_epoch=request.revision_epoch,
            status=RevisionExecutionStatus.COMPLETED,
            base_artifacts=request.base_artifacts,
            result_artifacts=[artifact],
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id=finding_id,
                    outcome=RevisionFindingOutcome.RESOLVED,
                    reason=(
                        "Reconstructed from a PMP-valid legacy Researcher Revision reply"
                    ),
                    result_artifact_ids=[report.research_report_id],
                )
                for finding_id in request.source_finding_ids
            ],
            human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            provider_reservation_ids=[],
            retrieval_reservation_ids=[],
            provider_call_count=0,
            retrieval_call_count=0,
            completed_at=handoff.metadata.updated_at,
        )
        message_id = str(
            uuid5(NAMESPACE_URL, f"{request.revision_request_id}:result-message")
        )
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=request_message.message_id,
                    sender_agent_id="researcher.manager",
                    receiver_agent_id=self.agent_id,
                    message_type=MessageType.REVISION_RESULT,
                    objective="Adapt the validated legacy Researcher reply to revision.v1",
                    payload=result.model_dump(mode="json"),
                    routing=PMPRouting(revision_target=None, reply_required=False),
                    metadata=PMPMetadata(
                        created_at=handoff.metadata.updated_at,
                        updated_at=handoff.metadata.updated_at,
                        status=MessageStatus.COMPLETED,
                        extensions={
                            "compatibility_adapter": "legacy_research_revision_result"
                        },
                    ),
                ).model_dump(mode="json"),
                "message_id": message_id,
            }
        )
        self.revision_exchange.create_result_once(request_message, message)
        return message

    async def recover(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        state = self.repository.load(workflow_id)
        if (
            not self.demo_safe_mode
            and state.revision_control.phase
            == RevisionControlPhase.AUTHORIZATION_REQUIRED.value
        ):
            return await self.revise(
                workflow_id,
                actor_id="cli.operator",
                actor_source="CLI",
                reason="Operator recovery authorized the saved Deliberation Revision",
                progress_callback=progress_callback,
            )
        self._repair_unexecuted_revision_after_cross_revision_replay(state)
        saved_review: DeliberationQualityReviewOutput | None = None
        if state.review_result is not None:
            try:
                saved_review = DeliberationQualityReviewOutput.model_validate(
                    state.review_result
                )
            except Exception:
                saved_review = None
        safe_mode_internal_revision_resume = (
            not self.demo_safe_mode
            and state.status == WorkflowStatus.BLOCKED.value
            and saved_review is not None
            and saved_review.status == QualityGateDecision.REVISION_REQUIRED.value
            and "Demo Safe Mode stopped automatic internal revision"
            in str((state.error or {}).get("message") or "")
        )
        blocked_review_rerecovery = (
            state.status == WorkflowStatus.BLOCKED.value
            and state.review_result is not None
            and not safe_mode_internal_revision_resume
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
        if (
            state.status == WorkflowStatus.BLOCKED.value
            and not blocked_review_rerecovery
            and not safe_mode_internal_revision_resume
        ):
            raise ValueError(
                "Blocked Deliberation workflows are not eligible for checkpoint recovery"
            )

        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime_config = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime_config.revision_limit is not None:
            self.max_revisions = runtime_config.revision_limit
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())

        self._preserve_legacy_manager_transport_failure(state)
        manager_failure = self._current_manager_provider_failure(state)
        if manager_failure is not None:
            provider_id = getattr(self.registry.provider, "provider_id", None)
            contract_repair = (
                self.provider_contract_repair_store.for_original_task(
                    workflow_id=state.workflow_id,
                    provider_id=provider_id,
                    original_task_id=manager_failure["logical_task_id"],
                )
                if isinstance(provider_id, str)
                else None
            )
            authorization = (
                self.provider_retry_store.for_original_task(
                    workflow_id=state.workflow_id,
                    provider_id=provider_id,
                    original_task_id=manager_failure["logical_task_id"],
                )
                if isinstance(provider_id, str)
                else None
            )
            if (
                contract_repair is not None
                and contract_repair.status
                == ProviderContractRepairStatus.PENDING.value
            ):
                pass
            elif authorization is None:
                return await self._fail(
                    state,
                    "Checkpoint recovery found an ambiguous Deliberation Manager "
                    "provider call without a reusable response; explicit provider "
                    "retry authorization is required for: "
                    + manager_failure["logical_task_id"],
                    progress_callback,
                )
            elif authorization.status != ProviderRetryStatus.PENDING.value:
                return await self._fail(
                    state,
                    "The one-time Deliberation Manager provider retry was already "
                    "consumed and no reusable response was persisted for: "
                    + manager_failure["logical_task_id"],
                    progress_callback,
                )
        else:
            review_error = self._latest_retryable_review_error(state)
            if review_error is not None:
                reviewer = self.registry.get(QUALITY_REVIEWER_ID)
                provider_id = getattr(reviewer.provider, "provider_id", None)
                original_task_id = str(review_error.payload.get("task_id") or "")
                authorization = (
                    self.provider_retry_store.for_original_task(
                        workflow_id=state.workflow_id,
                        provider_id=provider_id,
                        original_task_id=original_task_id,
                    )
                    if isinstance(provider_id, str) and original_task_id
                    else None
                )
                if authorization is None:
                    return await self._fail(
                        state,
                        "Checkpoint recovery found an ambiguous Deliberation Quality "
                        "Reviewer provider call without a reusable response; explicit "
                        "provider retry authorization is required for: "
                        + original_task_id,
                        progress_callback,
                    )
                if authorization.status != ProviderRetryStatus.PENDING.value:
                    return await self._fail(
                        state,
                        "The one-time Deliberation Quality Reviewer provider retry "
                        "was already consumed and no reusable response was persisted for: "
                        + original_task_id,
                        progress_callback,
                    )

        try:
            handoff = PMPMessage.model_validate(state.researcher_handoff)
            report = self._validate_researcher_handoff(handoff, allow_revision=True)
            if handoff.workflow_id != state.workflow_id or report.workflow_id != state.workflow_id:
                raise ValueError("Saved Researcher handoff does not match the workflow ID")
            state.research_report = report.model_dump(mode="json")
            tasks = self._prepare_revision_recovery_tasks(state, report)
        except Exception as exc:
            return await self._fail(
                state,
                f"Deliberation recovery could not validate the saved upstream state: {exc}",
                progress_callback,
            )

        self._preserve_legacy_manager_validation_marker(state)
        state.status = WorkflowStatus.RUNNING
        state.error = None
        state.completed_at = None
        state.current_agent_ids = []
        self.repository.save(state)
        await self._emit(progress_callback, f"Deliberation checkpoint recovery開始: {workflow_id}")

        valid_primary_ids: set[str] = set()
        incomplete_tasks: list[DeliberationAnalysisTask] = []
        downstream_exists = state.initial_integration is not None
        required_primary_targets = self._current_revision_primary_targets(state)
        for task in tasks:
            recovered_payload = self._recover_saved_invalid_analysis(state, task)
            if recovered_payload is not None:
                state.analysis_results[task.target_agent_id] = recovered_payload
                state.checkpoint_revisions[f"primary:{task.target_agent_id}"] = (
                    state.revision_count
                )
                if task.target_agent_id not in state.completed_agents:
                    state.completed_agents.append(task.target_agent_id)
                if task.target_agent_id in state.failed_agents:
                    state.failed_agents.remove(task.target_agent_id)
                self.repository.save(state)
            payload = state.analysis_results.get(task.target_agent_id)
            if payload is not None and self._saved_analysis_is_valid(state, task, payload):
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
                and task.target_agent_id not in required_primary_targets
            )
            if not tolerated_failure:
                incomplete_tasks.append(task)

        if len(valid_primary_ids) >= 2 and downstream_exists:
            incomplete_tasks = [
                task
                for task in incomplete_tasks
                if task.target_agent_id not in state.failed_agents
                or task.target_agent_id in required_primary_targets
            ]

        if incomplete_tasks:
            ambiguous = [
                task
                for task in incomplete_tasks
                if state.revision_count > 0
                and self._task_has_provider_attempt(state, task.task_id)
                and not self._task_has_contract_repairable_failure(state, task.task_id)
            ]
            if ambiguous:
                return await self._fail(
                    state,
                    "Checkpoint recovery found revision provider calls without a reusable "
                    "response; explicit provider retry authorization is required for: "
                    + ", ".join(task.target_agent_id for task in ambiguous),
                    progress_callback,
                )
            recovery_tasks = [
                self._make_contract_repair_task(state, task)
                if self._task_has_contract_repairable_failure(state, task.task_id)
                else self._mark_recovery_task(task)
                if state.revision_count > 0
                else self._make_recovery_task(task)
                for task in incomplete_tasks
            ]
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

        missing_required_targets = sorted(
            target
            for target in required_primary_targets
            if state.checkpoint_revisions.get(f"primary:{target}", -1)
            < state.revision_count
        )
        if missing_required_targets:
            return await self._fail(
                state,
                "Required revision analyses remain incomplete after recovery: "
                + ", ".join(missing_required_targets),
                progress_callback,
            )

        valid_primary_count = sum(
            1
            for task in self._recovery_primary_tasks(state, report)
            if (payload := state.analysis_results.get(task.target_agent_id)) is not None
            and self._saved_analysis_is_valid(state, task, payload)
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
            counterargument = CounterargumentAnalysisResult.model_validate(
                state.counterargument_analysis
            )
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

        if self._recover_saved_final_challenge_traceability(state, counterargument):
            self._clear_recovery_checkpoints(state, "deterministic_validation")
            state.deliberation_result = None
            self.repository.save(state)
            await self._emit(
                progress_callback,
                "Saved Final traceability repaired without a Provider call; "
                "restarting at deterministic validation",
            )
            return await self._integrate_and_review(
                state,
                rerun_initial=False,
                rerun_counterargument=False,
                rerun_final=False,
                rerun_validation=True,
                progress_callback=progress_callback,
                recovery=True,
                reuse_saved_review_response=False,
                review_task_variant="traceability_contract_repair_1",
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
                reuse_saved_review_response=False,
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
        report = ResearchReport.model_validate(
            canonicalize_legacy_trace_ids(raw_report)
        )
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
            # A revision_required AI assessment can cross this boundary only when
            # a separate Human Evidence Decision accepts every sufficiency gap.
            if quality.get("status") != "revision_required":
                raise ValueError("Research Report did not pass the Researcher Quality Gate")
        raw_decision = handoff.payload.get("human_evidence_decision")
        if raw_decision is None:
            raise ValueError("Researcher handoff has no Human Evidence Decision")
        decision = HumanEvidenceDecision.model_validate(raw_decision)
        if decision.workflow_id != handoff.workflow_id:
            raise ValueError("Human Evidence Decision workflow_id mismatch")
        if decision.decision not in {
            HumanEvidenceDecisionType.ACCEPT.value,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS.value,
        }:
            raise ValueError("Human Evidence Decision does not permit downstream handoff")
        accepted_gaps = [
            AcceptedEvidenceGap.model_validate(item)
            for item in handoff.payload.get("accepted_evidence_gaps", [])
        ]
        accepted_ids = {item.finding_id for item in accepted_gaps}
        if accepted_ids != set(decision.accepted_finding_ids):
            raise ValueError("Accepted Evidence Gaps do not match the Human Evidence Decision")
        repairs = [
            self._parse_human_evidence_integrity_repair(item)
            for item in handoff.payload.get("human_evidence_integrity_repairs", [])
        ]
        repaired_ids = {item.finding_id for item in repairs}
        finding_ids = {
            str(item.get("finding_id"))
            for item in quality.get("findings", [])
            if item.get("finding_id")
        }
        if finding_ids - accepted_ids - repaired_ids:
            raise ValueError(
                "Researcher handoff contains a Quality Finding without an accepted gap "
                "or deterministic integrity repair"
            )
        if quality.get("status") == "revision_required" and (
            decision.decision != HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS.value
            or not accepted_ids
        ):
            raise ValueError(
                "revision_required research may proceed only through "
                "ACCEPT_WITH_LIMITATIONS"
            )
        return report

    @staticmethod
    def _research_context_from_state(
        state: DeliberationWorkflowState,
        report: ResearchReport,
    ) -> DeliberationResearchContext:
        payload = state.researcher_handoff.get("payload") or {}
        decision = payload.get("human_evidence_decision")
        return build_deliberation_research_context(
            report,
            human_evidence_decision=(
                HumanEvidenceDecision.model_validate(decision) if decision else None
            ),
            accepted_evidence_gaps=[
                AcceptedEvidenceGap.model_validate(item)
                for item in payload.get("accepted_evidence_gaps", [])
            ],
            human_evidence_integrity_repairs=[
                validate_human_evidence_integrity_repair(item)
                for item in payload.get("human_evidence_integrity_repairs", [])
            ],
        )

    @staticmethod
    def _parse_human_evidence_integrity_repair(
        item: object,
    ) -> HumanEvidenceIntegrityRepair:
        return validate_human_evidence_integrity_repair(item)

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

    @classmethod
    def _stale_primary_targets(
        cls,
        state: DeliberationWorkflowState,
        report: ResearchReport,
    ) -> list[str]:
        """Close revision routing over analyses invalidated by a new Evidence set."""

        report_evidence_ids = {item.evidence_id for item in report.evidence_items}
        stale: list[str] = []
        for agent_id in PRIMARY_ANALYST_IDS:
            payload = state.analysis_results.get(agent_id)
            if payload is None:
                stale.append(agent_id)
                continue
            referenced = cls._collect_evidence_ids(payload)
            if not referenced or referenced - report_evidence_ids:
                stale.append(agent_id)
        return stale

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
                error = self._validate_analysis_response(state, task, request, response)
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
        state: DeliberationWorkflowState,
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
        source_error = self._validate_analysis_source_bindings(state, response.payload)
        if source_error:
            return f"{task.target_agent_id} {source_error}"
        return None

    @staticmethod
    def _preserve_legacy_manager_validation_marker(
        state: DeliberationWorkflowState,
    ) -> None:
        message = str((state.error or {}).get("message") or "")
        for stage, schema_name in (
            ("initial_integration", "InitialIntegratedAnalysis"),
            ("final_integration", "FinalIntegratedAnalysis"),
        ):
            if f"validation error for {schema_name}" not in message:
                continue
            logical_task_id = (
                f"deliberation_manager_{stage}_revision_{state.revision_count}"
                "_recovery_1"
            )
            state.manager_invalid_payloads.setdefault(
                logical_task_id,
                {
                    "stage": stage,
                    "output_schema": schema_name,
                    "invalid_payload": None,
                    "validation_errors": [],
                    "recorded_at": utc_now().isoformat(),
                    "legacy_raw_payload_unavailable": True,
                },
            )

    def _preserve_legacy_manager_transport_failure(
        self,
        state: DeliberationWorkflowState,
    ) -> bool:
        """Materialize pre-Cycle-012 IncompleteRead state as an audit record."""

        message = str((state.error or {}).get("message") or "")
        if "IncompleteRead(" not in message:
            return False
        if state.initial_integration is None:
            stage = "initial_integration"
        elif state.final_integration is None:
            stage = "final_integration"
        else:
            return False

        base = (
            f"deliberation_manager_{stage}_revision_{state.revision_count}"
            "_recovery_1"
        )
        contract_repair = f"{base}_contract_repair_1"
        provider_id = getattr(self.registry.provider, "provider_id", None)
        candidates = [contract_repair, base]
        logical_task_id = next(
            (
                candidate
                for candidate in candidates
                if isinstance(provider_id, str)
                and self.provider_retry_store.reservation_path(
                    provider_id=provider_id,
                    workflow_id=state.workflow_id,
                    task_id=candidate,
                ).exists()
            ),
            base,
        )
        if any(
            item.get("logical_task_id") == logical_task_id
            for item in state.manager_provider_failures
        ):
            return False
        digest = hashlib.sha256(
            f"{state.workflow_id}|{logical_task_id}|{message}".encode("utf-8")
        ).hexdigest()[:24]
        state.manager_provider_failures.append(
            {
                "failure_id": f"manager_provider_failure_legacy_{digest}",
                "logical_task_id": logical_task_id,
                "stage": stage,
                "error_class": "RetryableAgentError",
                "error_message": "OpenRouter response body was interrupted before completion",
                "root_exception_type": "IncompleteRead",
                "retry_count": 0,
                "automatic_retry_allowed": False,
                "provider": "openrouter",
                "model_id": None,
                "recorded_at": utc_now().isoformat(),
                "compatibility_source": "pre_cycle_012_state_error",
            }
        )
        return True

    @staticmethod
    def _current_manager_provider_failure(
        state: DeliberationWorkflowState,
    ) -> dict[str, Any] | None:
        for failure in reversed(state.manager_provider_failures):
            stage = failure.get("stage")
            if stage == "initial_integration" and state.initial_integration is None:
                return failure
            if stage == "final_integration" and state.final_integration is None:
                return failure
        return None

    @staticmethod
    def _latest_retryable_review_error(
        state: DeliberationWorkflowState,
    ) -> PMPMessage | None:
        if (
            state.final_integration is None
            or state.deterministic_validation is None
            or state.review_result is not None
        ):
            return None
        return next(
            (
                message
                for message in reversed(state.message_history)
                if message.sender_agent_id == QUALITY_REVIEWER_ID
                and message.receiver_agent_id == "deliberation.manager"
                and message.message_type == MessageType.ERROR.value
                and message.payload.get("error_class") == "RetryableAgentError"
            ),
            None,
        )

    @staticmethod
    def _analysis_schema(agent_id: str):
        return {
            "deliberation.argument_analyst": ArgumentAnalysisResult,
            "deliberation.causal_structural_analyst": CausalStructuralAnalysisResult,
            "deliberation.stakeholder_response_analyst": StakeholderResponseAnalysisResult,
        }[agent_id]

    @staticmethod
    def _normalize_saved_analysis_payload(
        agent_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = canonicalize_legacy_trace_ids(payload)
        if agent_id == "deliberation.causal_structural_analyst":
            return canonicalize_legacy_causal_item_ids(normalized)
        return normalized

    def _saved_analysis_is_valid(
        self,
        state: DeliberationWorkflowState,
        task: DeliberationAnalysisTask,
        payload: dict[str, Any],
    ) -> bool:
        try:
            normalized_payload = self._normalize_saved_analysis_payload(
                task.target_agent_id,
                payload,
            )
            result = self._analysis_schema(task.target_agent_id).model_validate(
                normalized_payload
            )
        except Exception:
            return False
        if result.task_id != task.task_id:
            return False
        if self._collect_evidence_ids(normalized_payload) - set(task.target_evidence_ids):
            return False
        return self._validate_analysis_source_bindings(state, normalized_payload) is None

    def _recovery_primary_tasks(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
    ) -> list[DeliberationAnalysisTask]:
        report_evidence_ids = {item.evidence_id for item in report.evidence_items}
        tasks_by_agent: dict[str, DeliberationAnalysisTask] = {}

        def consider(raw: dict[str, Any]) -> None:
            try:
                task = DeliberationAnalysisTask.model_validate(
                    canonicalize_legacy_trace_ids(raw)
                )
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

    def _prepare_revision_recovery_tasks(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
    ) -> list[DeliberationAnalysisTask]:
        tasks = self._recovery_primary_tasks(state, report)
        if state.revision_count <= 0:
            return tasks
        stale_targets = self._stale_primary_targets(state, report)
        if not stale_targets:
            return tasks

        record = next(
            (
                item
                for item in reversed(state.revision_history)
                if item.iteration == state.revision_count
            ),
            None,
        )
        if record is not None:
            record.target_agent_ids = list(
                dict.fromkeys([*record.target_agent_ids, *stale_targets])
            )
            record.rerun_stages = self._revision_stages(record.target_agent_ids)

        review_id = str((state.review_result or {}).get("review_id") or "legacy_review")
        prepared: list[DeliberationAnalysisTask] = []
        for task in tasks:
            if task.target_agent_id not in stale_targets:
                prepared.append(task)
                continue
            if self._task_has_provider_attempt(state, task.task_id):
                prepared.append(task)
                continue
            raw = task.model_dump(mode="json")
            raw["task_id"] = self._revision_task_id(state, task.target_agent_id)
            raw["revision_context"] = {
                "iteration": state.revision_count,
                "review_id": review_id,
                "dependency_closure": "upstream_evidence_set_changed",
                "checkpoint_recovery": True,
            }
            prepared.append(DeliberationAnalysisTask.model_validate(raw))
        self._replace_analysis_tasks(state, prepared)
        return prepared

    @staticmethod
    def _current_revision_primary_targets(
        state: DeliberationWorkflowState,
    ) -> set[str]:
        return {
            target
            for record in state.revision_history
            if record.iteration == state.revision_count
            for target in record.target_agent_ids
            if target in PRIMARY_ANALYST_IDS
        }

    def _recover_saved_invalid_analysis(
        self,
        state: DeliberationWorkflowState,
        task: DeliberationAnalysisTask,
    ) -> dict[str, Any] | None:
        requests = [
            message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == task.target_agent_id
            and message.message_type == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            and message.payload.get("task_id") == task.task_id
        ]
        for request in reversed(requests):
            for response in reversed(state.message_history):
                if (
                    response.parent_message_id != request.message_id
                    or response.sender_agent_id != task.target_agent_id
                    or response.receiver_agent_id != self.agent_id
                ):
                    continue
                if response.message_type == MessageType.ERROR.value:
                    saved_payload = response.payload.get("invalid_payload")
                elif (
                    response.message_type
                    == MessageType.DELIBERATION_TASK_RESULT.value
                ):
                    saved_payload = response.payload
                else:
                    continue
                if not isinstance(saved_payload, dict):
                    continue
                normalized_payload = self._normalize_saved_analysis_payload(
                    task.target_agent_id,
                    saved_payload,
                )
                normalized_payload, repaired_source_bindings = (
                    self._repair_saved_stakeholder_source_bindings(
                        state,
                        task.target_agent_id,
                        normalized_payload,
                    )
                )
                try:
                    result = self._analysis_schema(task.target_agent_id).model_validate(
                        normalized_payload
                    )
                except Exception:
                    continue
                if result.task_id != task.task_id:
                    continue
                if self._collect_evidence_ids(normalized_payload) - set(
                    task.target_evidence_ids
                ):
                    continue
                if self._validate_analysis_source_bindings(
                    state, normalized_payload
                ) is not None:
                    continue
                if (
                    response.message_type
                    == MessageType.DELIBERATION_TASK_RESULT.value
                    and not repaired_source_bindings
                ):
                    return result.model_dump(mode="json")
                recovered_response = PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=request.message_id,
                    sender_agent_id=task.target_agent_id,
                    receiver_agent_id=self.agent_id,
                    message_type=MessageType.DELIBERATION_TASK_RESULT,
                    objective=(
                        "Recover persisted analysis after authoritative source binding"
                        if repaired_source_bindings
                        else "Recover persisted analysis after trace ID compatibility validation"
                    ),
                    payload=result.model_dump(mode="json"),
                    constraints=request.constraints,
                    context=PMPContext(
                        current_stage=task.target_agent_id,
                        previous_stage=request.context.current_stage,
                        next_stage=self.agent_id,
                    ),
                    metadata=PMPMetadata(
                        status=MessageStatus.COMPLETED,
                        retry_count=response.metadata.retry_count,
                        notes=(
                            "Recovered from persisted invalid_payload without a provider call; "
                            "source_ids were rebound from the saved ResearchReport evidence map"
                            if repaired_source_bindings
                            else "Recovered from persisted invalid_payload without a provider call"
                        ),
                        extensions=response.metadata.extensions,
                    ),
                )
                self.pmp_validator.validate(recovered_response)
                state.message_history.append(recovered_response)
                return result.model_dump(mode="json")
        return None

    @staticmethod
    def _task_has_provider_attempt(
        state: DeliberationWorkflowState,
        task_id: str,
    ) -> bool:
        return any(
            message.sender_agent_id == "deliberation.manager"
            and message.message_type == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            and message.payload.get("task_id") == task_id
            for message in state.message_history
        )

    @staticmethod
    def _task_has_contract_repairable_failure(
        state: DeliberationWorkflowState,
        task_id: str,
    ) -> bool:
        request_ids = {
            message.message_id
            for message in state.message_history
            if message.sender_agent_id == "deliberation.manager"
            and message.message_type == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            and message.payload.get("task_id") == task_id
        }
        return any(
            message.parent_message_id in request_ids
            and message.message_type == MessageType.ERROR.value
            and message.payload.get("error_class") == "PayloadValidationError"
            and isinstance(message.payload.get("invalid_payload"), dict)
            for message in state.message_history
        )

    def _make_contract_repair_task(
        self,
        state: DeliberationWorkflowState,
        task: DeliberationAnalysisTask,
    ) -> DeliberationAnalysisTask:
        raw = task.model_dump(mode="json")
        identity = (
            f"{state.workflow_id}|revision={state.revision_count}|"
            f"{task.target_agent_id}|contract_repair=1"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        raw["task_id"] = (
            f"delib_task_r{state.revision_count}_{digest}_contract_repair_1"
        )
        raw["revision_context"] = {
            **(task.revision_context or {}),
            "checkpoint_recovery": True,
            "contract_repair": True,
            "previous_task_id": task.task_id,
            "validation_failures": self._saved_analysis_validation_failures(
                state, task
            ),
            "repair_requirements": self._analysis_contract_repair_requirements(
                task.target_agent_id
            ),
        }
        return DeliberationAnalysisTask.model_validate(raw)

    @staticmethod
    def _analysis_contract_repair_requirements(agent_id: str) -> list[str]:
        common = [
            "analysis_id must include a unique non-empty suffix after its type prefix",
            "do not use a bare namespace prefix as a complete identifier",
        ]
        specific = {
            "deliberation.argument_analyst": [
                "all classified, mapped, premise, warrant, and gap claim references must name a central_claims.claim_id returned in this response",
                "every central claim must have exactly one or more evidence_mappings entries",
            ],
            "deliberation.causal_structural_analyst": [
                "every causal or structural item_id must be unique and include a non-empty suffix after the required namespace prefix",
                "evidence_mappings.mapped_item_ids must reuse the exact complete item_ids returned in this response",
            ],
            "deliberation.stakeholder_response_analyst": [
                "specific_facts.verification_status must be exactly one of verified, inferred, unknown, or unverified and must not be translated",
            ],
        }
        return [*common, *specific.get(agent_id, [])]

    @staticmethod
    def _saved_analysis_validation_failures(
        state: DeliberationWorkflowState,
        task: DeliberationAnalysisTask,
    ) -> list[str]:
        request_ids = {
            message.message_id
            for message in state.message_history
            if message.sender_agent_id == "deliberation.manager"
            and message.receiver_agent_id == task.target_agent_id
            and message.message_type == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            and message.payload.get("task_id") == task.task_id
        }
        failures: list[str] = []
        for message in state.message_history:
            if (
                message.parent_message_id not in request_ids
                or message.sender_agent_id != task.target_agent_id
                or message.message_type != MessageType.ERROR.value
            ):
                continue
            path = message.payload.get("validation_field_path")
            if isinstance(path, str) and path.strip():
                failures.append(path.strip())
            elif isinstance(message.payload.get("message"), str):
                failures.append(str(message.payload["message"]).splitlines()[0])
        return list(dict.fromkeys(failures))

    @staticmethod
    def _mark_recovery_task(
        task: DeliberationAnalysisTask,
    ) -> DeliberationAnalysisTask:
        raw = task.model_dump(mode="json")
        raw["revision_context"] = {
            **(task.revision_context or {}),
            "checkpoint_recovery": True,
        }
        return DeliberationAnalysisTask.model_validate(raw)

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

    def _normalized_primary_analyses(
        self,
        state: DeliberationWorkflowState,
    ) -> dict[str, dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}
        for agent_id, payload in state.analysis_results.items():
            candidate = (
                canonicalize_legacy_causal_item_ids(payload)
                if agent_id == "deliberation.causal_structural_analyst"
                else payload
            )
            normalized[agent_id] = (
                self._analysis_schema(agent_id)
                .model_validate(candidate)
                .model_dump(mode="json")
                if agent_id in PRIMARY_ANALYST_IDS
                else candidate
            )
        return normalized

    async def _integrate_manager_stage(
        self,
        state: DeliberationWorkflowState,
        *,
        input_data: dict[str, Any],
        output_schema: type[BaseModel],
        stage: str,
        recovery: bool,
    ) -> BaseModel:
        recovered = self._recover_saved_manager_payload(
            state,
            output_schema=output_schema,
            stage=stage,
        )
        if recovered is not None:
            return recovered

        logical_task_id = self._manager_integration_task_id(
            state,
            stage,
            recovery=recovery,
        )
        model_override = None
        if logical_task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX):
            provider_id = getattr(self.registry.provider, "provider_id", None)
            if not isinstance(provider_id, str):
                raise ValueError("Deliberation Manager provider ID is unavailable")
            original_task_id = logical_task_id[: -len(PROVIDER_CONTRACT_REPAIR_SUFFIX)]
            repair = self.provider_contract_repair_store.for_original_task(
                workflow_id=state.workflow_id,
                provider_id=provider_id,
                original_task_id=original_task_id,
            )
            if repair is None:
                raise ValueError("Deliberation Manager contract repair is missing")
            model_override = repair.repair_model_id
        try:
            result = await self.registry.integrate(
                input_data=input_data,
                output_schema=output_schema,
                stage=stage,
                workflow_id=state.workflow_id,
                recovery=recovery,
                logical_task_id=logical_task_id,
                model_override=model_override,
            )
            provenance_errors = integration_provenance_errors(
                result.model_dump(mode="json"),
                input_data,
            )
            if provenance_errors:
                raise PayloadValidationError(
                    "Deliberation Manager returned provenance IDs that do not "
                    "belong to the source analysis artifacts in its request",
                    invalid_payload=result.model_dump(mode="json"),
                    validation_errors=provenance_errors,
                )
            return result
        except PayloadValidationError as exc:
            if isinstance(exc.invalid_payload, dict):
                state.manager_invalid_payloads[logical_task_id] = {
                    "stage": stage,
                    "output_schema": output_schema.__name__,
                    "invalid_payload": exc.invalid_payload,
                    "validation_errors": exc.validation_errors,
                    "recorded_at": utc_now().isoformat(),
                }
                self.repository.save(state)
            raise
        except RetryableAgentError as exc:
            root = exc
            visited: set[int] = set()
            while id(root) not in visited:
                visited.add(id(root))
                next_error = root.__cause__ or root.__context__
                if not isinstance(next_error, Exception):
                    break
                root = next_error
            state.manager_provider_failures.append(
                {
                    "failure_id": new_id("manager_provider_failure"),
                    "logical_task_id": logical_task_id,
                    "stage": stage,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc),
                    "root_exception_type": type(root).__name__,
                    "retry_count": max(int(exc.retry_count), 0),
                    "automatic_retry_allowed": bool(
                        exc.automatic_retry_allowed
                    ),
                    "provider": exc.provider or type(self.registry.provider).__name__,
                    "model_id": exc.model_id,
                    "recorded_at": utc_now().isoformat(),
                    "compatibility_source": None,
                }
            )
            self.repository.save(state)
            raise

    def _recover_saved_manager_payload(
        self,
        state: DeliberationWorkflowState,
        *,
        output_schema: type[BaseModel],
        stage: str,
    ) -> BaseModel | None:
        causal_payload = state.analysis_results.get(
            "deliberation.causal_structural_analyst"
        )
        for logical_task_id, record in reversed(
            list(state.manager_invalid_payloads.items())
        ):
            if record.get("stage") != stage:
                continue
            payload_revision = self._manager_payload_revision(logical_task_id)
            if (
                payload_revision is not None
                and payload_revision != state.revision_count
            ):
                continue
            raw = record.get("invalid_payload")
            if not isinstance(raw, dict):
                continue
            normalized = deepcopy(raw)
            if isinstance(causal_payload, dict):
                normalized = canonicalize_legacy_causal_references(
                    normalized,
                    causal_payload,
                )
            recovery_audit: dict[str, Any] = {}
            if stage == "final_integration":
                try:
                    normalized, recovery_audit = (
                        self._normalize_saved_final_integration_payload(
                            state,
                            normalized,
                            logical_task_id=logical_task_id,
                        )
                    )
                except ValueError:
                    continue
            removed_change_ids: list[str] = []
            if stage == "initial_integration":
                for entry in normalized.get("traceability_index", []):
                    if not isinstance(entry, dict):
                        continue
                    removed_change_ids.extend(
                        item
                        for item in entry.get("integration_change_ids", [])
                        if isinstance(item, str)
                    )
                    entry["integration_change_ids"] = []
            try:
                result = output_schema.model_validate(normalized)
            except Exception:
                continue
            provenance_input = self._manager_provenance_input(state, stage)
            if integration_provenance_errors(
                result.model_dump(mode="json"),
                provenance_input,
            ):
                # A saved payload may be syntactically valid while referring to
                # another analysis role. Rebinding after the fact would claim
                # provenance the original provider invocation did not have, so
                # recovery must use a distinct Manager task identity instead.
                continue
            if stage == "final_integration":
                counterargument = CounterargumentAnalysisResult.model_validate(
                    state.counterargument_analysis
                )
                self._validate_final_counterargument_dispositions(
                    counterargument,
                    result,
                )
            if not any(
                item.get("logical_task_id") == logical_task_id
                for item in state.manager_payload_recoveries
            ):
                state.manager_payload_recoveries.append(
                    {
                        "logical_task_id": logical_task_id,
                        "stage": stage,
                        "output_schema": output_schema.__name__,
                        "recovered_at": utc_now().isoformat(),
                        "provider_call_reused": False,
                        "provider_call_count": 0,
                        "compatibility_adapter": "+".join(
                            [
                                "saved_causal_item_id_map",
                                *(
                                    ["drop_initial_dangling_change_refs"]
                                    if removed_change_ids
                                    else []
                                ),
                                *(
                                    ["saved_final_identifier_rebinding"]
                                    if recovery_audit
                                    else []
                                ),
                            ]
                        ),
                        "removed_dangling_integration_change_ids": list(
                            dict.fromkeys(removed_change_ids)
                        ),
                        **recovery_audit,
                    }
                )
            self.repository.save(state)
            return result
        return None

    def _manager_provenance_input(
        self,
        state: DeliberationWorkflowState,
        stage: str,
    ) -> dict[str, Any]:
        primary = self._normalized_primary_analyses(state)
        if stage == "initial_integration":
            return {"primary_analyses": primary}
        return {
            "primary_analysis_ids": {
                agent_id: payload.get("analysis_id")
                for agent_id, payload in primary.items()
            },
            "initial_integration": state.initial_integration,
            "counterargument_analysis": state.counterargument_analysis,
        }

    @staticmethod
    def _manager_payload_revision(logical_task_id: str) -> int | None:
        marker = "_revision_"
        if marker not in logical_task_id:
            return None
        suffix = logical_task_id.split(marker, 1)[1]
        digits = suffix.split("_", 1)[0]
        return int(digits) if digits.isdigit() else None

    def _repair_unexecuted_revision_after_cross_revision_replay(
        self,
        state: DeliberationWorkflowState,
    ) -> bool:
        """Undo only a never-executed max-limit plan caused by stale payload reuse."""

        if (
            state.status != WorkflowStatus.BLOCKED.value
            or state.revision_count < 1
            or "Quality Reviewerが" not in str((state.error or {}).get("message") or "")
            or "revision_requiredを返したため停止" not in str(
                (state.error or {}).get("message") or ""
            )
            or not state.revision_history
            or state.revision_history[-1].iteration != state.revision_count
        ):
            return False
        replay = next(
            (
                item
                for item in reversed(state.manager_payload_recoveries)
                if item.get("stage") == "final_integration"
                and "saved_final_identifier_rebinding"
                in str(item.get("compatibility_adapter") or "")
            ),
            None,
        )
        if replay is None:
            return False
        replay_revision = self._manager_payload_revision(
            str(replay.get("logical_task_id") or "")
        )
        checkpoint_revision = state.checkpoint_revisions.get("final_integration")
        if (
            replay_revision is None
            or checkpoint_revision is None
            or replay_revision == checkpoint_revision
            or checkpoint_revision != state.revision_count - 1
            or any(
                revision >= state.revision_count
                for revision in state.checkpoint_revisions.values()
            )
        ):
            return False
        revision_token = f"revision_{state.revision_count}"
        if any(
            revision_token in str(message.payload.get("task_id") or "")
            for message in state.message_history
        ):
            return False

        unexecuted_iteration = state.revision_count
        state.revision_history = [
            item
            for item in state.revision_history
            if item.iteration != unexecuted_iteration
        ]
        state.revision_count = checkpoint_revision
        self._clear_recovery_checkpoints(state, "final_integration")
        state.status = WorkflowStatus.FAILED
        state.error = {
            "message": (
                "Recovered a stale cross-revision final payload replay; "
                "resume the incomplete prior revision at final integration"
            )
        }
        state.manager_payload_recoveries.append(
            {
                "logical_task_id": (
                    f"deliberation_manager_final_integration_revision_"
                    f"{checkpoint_revision}_cross_revision_replay_repair"
                ),
                "stage": "final_integration",
                "output_schema": "FinalIntegratedAnalysis",
                "recovered_at": utc_now().isoformat(),
                "provider_call_count": 0,
                "compatibility_adapter": (
                    "rollback_unexecuted_revision_after_cross_revision_payload_reuse"
                ),
                "replayed_payload_revision": replay_revision,
                "invalid_checkpoint_revision": checkpoint_revision,
                "removed_unexecuted_revision_iteration": unexecuted_iteration,
            }
        )
        self.repository.save(state)
        return True

    def _normalize_saved_final_integration_payload(
        self,
        state: DeliberationWorkflowState,
        payload: dict[str, Any],
        *,
        logical_task_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Rebind a complete saved final payload to authoritative checkpoint IDs.

        This is a fail-closed read-compatibility adapter.  It never invents
        content, retries a Provider, or repairs a truncated/encoded object.  It
        only replaces placeholder identifiers in a complete persisted response
        using the saved initial integration, analyses, counterargument result,
        and Research Report.
        """

        required_objects = (
            "problem_definition",
            "causal_structure",
            "stakeholder_structure",
        )
        required_lists = (
            "key_claims",
            "major_viewpoints",
            "integration_changes",
            "counterargument_dispositions",
        )
        if any(not isinstance(payload.get(name), dict) for name in required_objects):
            raise ValueError("saved final integration is incomplete or encoded")
        if any(not isinstance(payload.get(name), list) for name in required_lists):
            raise ValueError("saved final integration is incomplete or encoded")
        if not payload["key_claims"] or not payload["integration_changes"]:
            raise ValueError("saved final integration has no recoverable content")

        initial = InitialIntegratedAnalysis.model_validate(state.initial_integration)
        counterargument = CounterargumentAnalysisResult.model_validate(
            state.counterargument_analysis
        )
        report = ResearchReport.model_validate(state.research_report)
        normalized = deepcopy(payload)

        workflow_token = state.workflow_id.replace("-", "_")
        digest = hashlib.sha256(
            f"{state.workflow_id}:{logical_task_id}".encode("utf-8")
        ).hexdigest()[:12]
        original_final_id = normalized.get("integration_id")
        if original_final_id == "integration_final_":
            normalized["integration_id"] = (
                f"integration_final_{workflow_token}_{digest}"
            )
        elif not (
            isinstance(original_final_id, str)
            and original_final_id.startswith("integration_final_")
            and len(original_final_id) > len("integration_final_")
        ):
            raise ValueError("saved final integration_id is not recoverable")

        original_previous_id = normalized.get("previous_integration_id")
        if original_previous_id == "integration_initial_":
            normalized["previous_integration_id"] = initial.integration_id
        elif original_previous_id != initial.integration_id:
            raise ValueError("saved final previous_integration_id is ambiguous")

        analysis_by_agent: dict[str, tuple[str, str]] = {}
        for agent_id, raw in state.analysis_results.items():
            if not isinstance(raw, dict):
                continue
            analysis_id = raw.get("analysis_id")
            task_id = raw.get("task_id")
            if isinstance(analysis_id, str) and isinstance(task_id, str):
                analysis_by_agent[agent_id] = (analysis_id, task_id)
        required_agents = (
            "deliberation.argument_analyst",
            "deliberation.causal_structural_analyst",
        )
        if any(agent_id not in analysis_by_agent for agent_id in required_agents):
            raise ValueError("saved final integration lacks authoritative analyses")
        identifier_map = {
            "arg_001": analysis_by_agent["deliberation.argument_analyst"][0],
            "causal_001": analysis_by_agent[
                "deliberation.causal_structural_analyst"
            ][0],
            "counter_001": counterargument.analysis_id,
        }
        normalized = self._replace_exact_identifiers(normalized, identifier_map)

        initial_claims = {
            item.claim_id: item for item in initial.key_claims
        }
        known_evidence_ids = {item.evidence_id for item in report.evidence_items}
        evidence_alias_candidates: dict[str, set[str]] = {}
        for raw_claim in normalized.get("key_claims", []):
            if not isinstance(raw_claim, dict):
                raise ValueError("saved final claim is not an object")
            claim_id = raw_claim.get("claim_id")
            initial_claim = initial_claims.get(claim_id)
            if initial_claim is None:
                raise ValueError("saved final claim has no initial lineage")
            candidates = set(initial_claim.evidence_ids) & known_evidence_ids
            if not candidates:
                raise ValueError("saved final claim has no authoritative evidence")
            evidence_ids = raw_claim.get("evidence_ids")
            if not isinstance(evidence_ids, list):
                raise ValueError("saved final claim evidence is encoded")
            for identifier in evidence_ids:
                if isinstance(identifier, str) and identifier not in known_evidence_ids:
                    evidence_alias_candidates.setdefault(identifier, set()).update(
                        candidates
                    )
        if evidence_alias_candidates:
            normalized = self._expand_saved_evidence_aliases(
                normalized,
                alias_candidates=evidence_alias_candidates,
                known_evidence_ids=known_evidence_ids,
            )

        initial_viewpoints = list(initial.candidate_viewpoints)
        viewpoint_map: dict[str, str] = {}
        used_initial_viewpoints: set[str] = set()
        for raw_viewpoint in normalized.get("major_viewpoints", []):
            if not isinstance(raw_viewpoint, dict):
                raise ValueError("saved final viewpoint is not an object")
            raw_id = raw_viewpoint.get("viewpoint_id")
            if not isinstance(raw_id, str):
                raise ValueError("saved final viewpoint has no identifier")
            if raw_id.startswith(("viewpoint_", "vp_")) and len(raw_id) > 3:
                used_initial_viewpoints.add(raw_id)
                continue
            raw_claim_ids = {
                item
                for item in raw_viewpoint.get("supporting_claim_ids", [])
                if isinstance(item, str)
            }
            ranked = sorted(
                (
                    (
                        len(raw_claim_ids & set(candidate.supporting_claim_ids)),
                        candidate.viewpoint_id,
                    )
                    for candidate in initial_viewpoints
                    if candidate.viewpoint_id not in used_initial_viewpoints
                ),
                reverse=True,
            )
            if not ranked or ranked[0][0] == 0:
                raise ValueError("saved final viewpoint lineage is ambiguous")
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                raise ValueError("saved final viewpoint lineage is ambiguous")
            viewpoint_map[raw_id] = ranked[0][1]
            used_initial_viewpoints.add(ranked[0][1])
        normalized = self._replace_exact_identifiers(normalized, viewpoint_map)

        alternative_map: dict[str, str] = {}
        preserved_interpretation_ids: list[str] = []
        alternatives = normalized["causal_structure"].get(
            "alternative_explanations", []
        )
        if not isinstance(alternatives, list):
            raise ValueError("saved final alternatives are encoded")
        for index, item in enumerate(alternatives, start=1):
            if not isinstance(item, dict):
                raise ValueError("saved final alternative is not an object")
            item_id = item.get("item_id")
            if isinstance(item_id, str) and item_id.startswith(
                ALTERNATIVE_INTERPRETATION_PREFIX
            ):
                preserved_interpretation_ids.append(item_id)
            elif isinstance(item_id, str) and item_id.startswith("alt_") and not (
                item_id.startswith("alt_exp_")
            ):
                alternative_map[item_id] = (
                    f"alternative_{workflow_token}_{index:03d}"
                )
        normalized = self._replace_exact_identifiers(normalized, alternative_map)

        changes = normalized["integration_changes"]
        dispositions = normalized["counterargument_dispositions"]
        counterarguments = list(counterargument.counterarguments)
        if len(dispositions) != len(counterarguments):
            raise ValueError("saved final disposition cardinality is ambiguous")
        disposition_change_counts: list[int] = []
        for disposition in dispositions:
            if not isinstance(disposition, dict) or not isinstance(
                disposition.get("integration_change_ids"), list
            ):
                raise ValueError("saved final disposition routing is encoded")
            disposition_change_counts.append(
                len(disposition["integration_change_ids"])
            )
        if sum(disposition_change_counts) != len(changes):
            raise ValueError("saved final change routing is ambiguous")

        change_ids: list[str] = []
        for index, change in enumerate(changes, start=1):
            if not isinstance(change, dict):
                raise ValueError("saved final integration change is not an object")
            change_id = f"change_{workflow_token}_{index:03d}"
            change["change_id"] = change_id
            change_ids.append(change_id)
        cursor = 0
        disposition_bindings: dict[str, list[str]] = {}
        for index, (disposition, source) in enumerate(
            zip(dispositions, counterarguments, strict=True)
        ):
            count = disposition_change_counts[index]
            assigned = change_ids[cursor : cursor + count]
            cursor += count
            disposition["counterargument_id"] = source.counterargument_id
            disposition["integration_change_ids"] = assigned
            disposition_bindings[source.counterargument_id] = assigned
            for change in changes[cursor - count : cursor]:
                change["source_counterargument_ids"] = [
                    source.counterargument_id
                ]

        normalized = self._expand_saved_counterargument_placeholders(
            normalized,
            counterargument_ids=[
                item.counterargument_id for item in counterarguments
            ],
        )

        normalized["traceability_index"] = []
        referenced_evidence_ids = self._collect_saved_evidence_references(normalized)
        unknown_evidence_ids = referenced_evidence_ids - known_evidence_ids
        if unknown_evidence_ids or not referenced_evidence_ids:
            raise ValueError(
                "saved final evidence aliases cannot be bound authoritatively"
            )
        source_by_evidence = {
            item.evidence_id: item.source_id for item in report.evidence_items
        }
        final_id = str(normalized["integration_id"])
        causal_item_ids: list[str] = []
        for field_name in (
            "causal_claims",
            "mechanisms",
            "structural_factors",
            "feedback_loops",
            "alternative_explanations",
        ):
            for item in normalized["causal_structure"].get(field_name, []):
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("item_id"), str)
                    and item["item_id"].startswith(FINAL_CAUSAL_TRACEABILITY_PREFIXES)
                ):
                    causal_item_ids.append(item["item_id"])
        analysis_ids = [
            value[0] for value in analysis_by_agent.values()
        ] + [counterargument.analysis_id]
        task_ids = [
            value[1] for value in analysis_by_agent.values()
        ] + [counterargument.task_id]
        normalized["traceability_index"] = [
            {
                "schema_version": "2.0",
                "claim_ids": [
                    item["claim_id"]
                    for item in normalized["key_claims"]
                    if isinstance(item, dict)
                    and isinstance(item.get("claim_id"), str)
                ],
                "viewpoint_ids": [
                    item["viewpoint_id"]
                    for item in normalized["major_viewpoints"]
                    if isinstance(item, dict)
                    and isinstance(item.get("viewpoint_id"), str)
                ],
                "causal_item_ids": list(dict.fromkeys(causal_item_ids)),
                "integration_change_ids": change_ids,
                "evidence_ids": sorted(referenced_evidence_ids),
                "source_ids": sorted(
                    {source_by_evidence[item] for item in referenced_evidence_ids}
                ),
                "analysis_ids": list(dict.fromkeys(analysis_ids)),
                "counterargument_ids": [
                    item.counterargument_id for item in counterarguments
                ],
                "challenge_ids": [
                    item.challenge_id for item in counterargument.steelman_arguments
                ],
                "integration_ids": [initial.integration_id, final_id],
                "task_ids": list(dict.fromkeys(task_ids)),
            }
        ]
        audit = {
            "rebound_analysis_aliases": identifier_map,
            "rebound_viewpoint_ids": viewpoint_map,
            "rebound_alternative_ids": alternative_map,
            "preserved_counterargument_interpretation_ids": list(
                dict.fromkeys(preserved_interpretation_ids)
            ),
            "rebound_change_count": len(change_ids),
            "rebound_disposition_change_ids": disposition_bindings,
            "evidence_alias_candidates": {
                key: sorted(value)
                for key, value in evidence_alias_candidates.items()
            },
            "traceability_rebuilt_from_authoritative_checkpoints": True,
        }
        return normalized, audit

    @classmethod
    def _expand_saved_evidence_aliases(
        cls,
        value: Any,
        *,
        alias_candidates: dict[str, set[str]],
        known_evidence_ids: set[str],
    ) -> Any:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if (
                    isinstance(item, list)
                    and (key.endswith("evidence_ids") or key == "evidence_linked")
                ):
                    expanded: list[Any] = []
                    for identifier in item:
                        if not isinstance(identifier, str):
                            expanded.append(identifier)
                        elif identifier in known_evidence_ids:
                            expanded.append(identifier)
                        elif identifier in alias_candidates:
                            expanded.extend(sorted(alias_candidates[identifier]))
                        else:
                            raise ValueError(
                                f"unknown saved evidence alias: {identifier}"
                            )
                    normalized[key] = list(dict.fromkeys(expanded))
                else:
                    normalized[key] = cls._expand_saved_evidence_aliases(
                        item,
                        alias_candidates=alias_candidates,
                        known_evidence_ids=known_evidence_ids,
                    )
            return normalized
        if isinstance(value, list):
            return [
                cls._expand_saved_evidence_aliases(
                    item,
                    alias_candidates=alias_candidates,
                    known_evidence_ids=known_evidence_ids,
                )
                for item in value
            ]
        return value

    @staticmethod
    def _collect_saved_evidence_references(value: Any) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                if (
                    isinstance(item, list)
                    and (key.endswith("evidence_ids") or key == "evidence_linked")
                ):
                    found.update(part for part in item if isinstance(part, str))
                else:
                    found.update(
                        DeliberationManager._collect_saved_evidence_references(item)
                    )
        elif isinstance(value, list):
            for item in value:
                found.update(
                    DeliberationManager._collect_saved_evidence_references(item)
                )
        return found

    @staticmethod
    def _expand_saved_counterargument_placeholders(
        value: Any,
        *,
        counterargument_ids: list[str],
    ) -> Any:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if key == "source_counterargument_ids" and isinstance(item, list):
                    expanded: list[Any] = []
                    for identifier in item:
                        if identifier == "counterargument_":
                            expanded.extend(counterargument_ids)
                        else:
                            expanded.append(identifier)
                    normalized[key] = list(dict.fromkeys(expanded))
                else:
                    normalized[key] = (
                        DeliberationManager._expand_saved_counterargument_placeholders(
                            item,
                            counterargument_ids=counterargument_ids,
                        )
                    )
            return normalized
        if isinstance(value, list):
            return [
                DeliberationManager._expand_saved_counterargument_placeholders(
                    item,
                    counterargument_ids=counterargument_ids,
                )
                for item in value
            ]
        return value

    @staticmethod
    def _replace_exact_identifiers(value: Any, mapping: dict[str, str]) -> Any:
        if isinstance(value, str):
            return mapping.get(value, value)
        if isinstance(value, list):
            return [
                DeliberationManager._replace_exact_identifiers(item, mapping)
                for item in value
            ]
        if isinstance(value, dict):
            return {
                key: DeliberationManager._replace_exact_identifiers(item, mapping)
                for key, item in value.items()
            }
        return value

    def _recover_saved_final_challenge_traceability(
        self,
        state: DeliberationWorkflowState,
        counterargument: CounterargumentAnalysisResult,
    ) -> bool:
        """Losslessly split known saved Challenge IDs from Counterargument IDs.

        This compatibility path is intentionally limited to saved checkpoints.
        New provider output must satisfy the schema and cross-artifact checks
        without normalization.
        """

        raw = state.final_integration
        if not isinstance(raw, dict):
            return False
        known_challenge_ids = {
            item.challenge_id for item in counterargument.steelman_arguments
        }
        if not known_challenge_ids:
            return False

        normalized = deepcopy(raw)
        moved_by_entry: dict[str, list[str]] = {}
        for index, entry in enumerate(normalized.get("traceability_index", [])):
            if not isinstance(entry, dict):
                continue
            raw_counterargument_ids = entry.get("counterargument_ids", [])
            if not isinstance(raw_counterargument_ids, list):
                continue
            moved = [
                identifier
                for identifier in raw_counterargument_ids
                if isinstance(identifier, str)
                and identifier in known_challenge_ids
            ]
            if not moved:
                continue
            entry["counterargument_ids"] = [
                identifier
                for identifier in raw_counterargument_ids
                if identifier not in known_challenge_ids
            ]
            existing_challenges = entry.get("challenge_ids", [])
            if not isinstance(existing_challenges, list):
                existing_challenges = []
            entry["challenge_ids"] = list(
                dict.fromkeys([*existing_challenges, *moved])
            )
            moved_by_entry[str(index)] = list(dict.fromkeys(moved))

        if not moved_by_entry:
            return False
        final = FinalIntegratedAnalysis.model_validate(normalized)
        self._validate_final_counterargument_dispositions(counterargument, final)
        invalidated_review_id = (
            str(state.review_result.get("review_id") or "")
            if isinstance(state.review_result, dict)
            else None
        )
        logical_task_id = f"{final.integration_id}_traceability_contract_repair_1"
        state.final_integration = final.model_dump(mode="json")
        if not any(
            item.get("logical_task_id") == logical_task_id
            for item in state.manager_payload_recoveries
        ):
            state.manager_payload_recoveries.append(
                {
                    "logical_task_id": logical_task_id,
                    "stage": "final_integration",
                    "output_schema": "FinalIntegratedAnalysis",
                    "recovered_at": utc_now().isoformat(),
                    "provider_call_count": 0,
                    "compatibility_adapter": (
                        "saved_final_challenge_reference_segregation"
                    ),
                    "moved_challenge_ids_by_traceability_entry": moved_by_entry,
                    "invalidated_review_id": invalidated_review_id,
                }
            )
        return True

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
        reuse_saved_review_response: bool = True,
        review_task_variant: str | None = None,
    ) -> DeliberationWorkflowState:
        report = ResearchReport.model_validate(state.research_report)
        research_context = self._research_context_from_state(state, report)
        while True:
            try:
                primary_for_integration = self._normalized_primary_analyses(state)
                if rerun_initial:
                    state.status = WorkflowStatus.INTEGRATING
                    initial = await self._integrate_manager_stage(
                        state,
                        input_data={
                            "research_report": research_context.model_dump(mode="json"),
                            "primary_analyses": primary_for_integration,
                            "previous_integration": state.initial_integration,
                            "revision_context": self._latest_revision_context(state),
                        },
                        output_schema=InitialIntegratedAnalysis,
                        stage="initial_integration",
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
                    final = await self._integrate_manager_stage(
                        state,
                        input_data={
                            "research_report": research_context.model_dump(mode="json"),
                            "primary_analysis_ids": {
                                agent_id: payload.get("analysis_id")
                                for agent_id, payload in primary_for_integration.items()
                            },
                            "initial_integration": initial.model_dump(mode="json"),
                            "counterargument_analysis": counterargument.model_dump(mode="json"),
                            "previous_final_integration": state.final_integration,
                            "revision_context": self._latest_revision_context(state),
                        },
                        output_schema=FinalIntegratedAnalysis,
                        stage="final_integration",
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
                    research_context,
                    recovery=recovery,
                    reuse_saved_response=reuse_saved_review_response,
                    task_variant=review_task_variant,
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
            if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
                self._finalize_internal_revision(
                    state,
                    review_response_id=review_response_id,
                    completed=True,
                    reason=review.reason,
                )
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
            if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
                self._finalize_internal_revision(
                    state,
                    review_response_id=review_response_id,
                    completed=False,
                    reason=review.reason,
                )
            return await self._block(state, review.reason, progress_callback), False, False

        if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
            self._finalize_internal_revision(
                state,
                review_response_id=review_response_id,
                completed=False,
                reason=review.reason,
            )

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

        if (
            self.demo_safe_mode
            and state.revision_control.phase
            == RevisionControlPhase.AUTHORIZATION_REQUIRED.value
        ):
            return (
                await self._block(
                    state,
                    "Demo Safe Mode retained the existing authorized-boundary Revision plan; "
                    "no Deliberation Agent was re-dispatched",
                    progress_callback,
                ),
                False,
                False,
            )
        self._store_pending_revision(state, review)
        request = self._plan_internal_revision(
            state,
            review,
            review_response_id=review_response_id,
            targets=list(review.revision_targets),
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
        authorization = self._create_revision_authorization(
            state,
            request,
            actor_id=self.agent_id,
            actor_source="SYSTEM",
            reason="Safe Mode is disabled; runtime policy permits one automatic Revision",
        )
        self.repository.save(state)
        return await self._execute_authorized_internal_revision(
            state,
            request=request,
            authorization=authorization,
            progress_callback=progress_callback,
        )

    def _plan_internal_revision(
        self,
        state: DeliberationWorkflowState,
        review: DeliberationQualityReviewOutput,
        *,
        review_response_id: str,
        targets: list[str],
    ) -> RevisionRequestV1:
        if not targets:
            raise ValueError("Deliberation internal Revision has no target")
        if state.final_integration is None:
            raise ValueError("Deliberation internal Revision has no final integration")
        revision_epoch = max(
            state.revision_count,
            state.revision_control.revision_epoch,
        ) + 1
        finding_ids = [item.finding_id for item in review.findings]
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.DELIBERATION,
            target_layer=LayerId.DELIBERATION,
            revision_epoch=revision_epoch,
            source_review_id=review_response_id,
            source_finding_ids=finding_ids,
        )
        final_id = str(state.final_integration.get("integration_id") or "")
        report_id = str(state.research_report.get("research_report_id") or "")
        parent_request_id = (
            state.revision_control.active_request_id
            if state.revision_control.active_request_id
            and state.revision_control.phase
            in {
                RevisionControlPhase.COMPLETED.value,
                RevisionControlPhase.WAITING_UPSTREAM_RESULT.value,
            }
            else None
        )
        request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.INTERNAL,
            source_layer=LayerId.DELIBERATION,
            target_layer=LayerId.DELIBERATION,
            revision_epoch=revision_epoch,
            root_revision_request_id=(
                (state.revision_control.root_revision_request_id or parent_request_id)
                if parent_request_id
                else request_id
            ),
            parent_revision_request_id=parent_request_id,
            source_review_id=review_response_id,
            source_finding_ids=finding_ids,
            target_agent_ids=targets,
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="deliberation.final_integration",
                    artifact_id=final_id,
                    sha256=canonical_sha256(state.final_integration),
                ),
                RevisionArtifactRef(
                    artifact_type="researcher.research_report",
                    artifact_id=report_id,
                    sha256=canonical_sha256(state.research_report),
                ),
            ],
            required_actions=list(
                dict.fromkeys(item.required_action for item in review.findings)
            ),
            acceptance_conditions=[
                f"{item.finding_id} is resolved, rejected, or retained as unresolved"
                for item in review.findings
            ],
            evidence_expansion_allowed=False,
            retrieval_allowed=False,
            expected_human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
        )
        review_message = next(
            (
                item
                for item in state.message_history
                if item.message_id == review_response_id
            ),
            None,
        )
        if review_message is None:
            raise ValueError("Deliberation Revision source review PMP is missing")
        execution_receiver = (
            targets[0]
            if targets[0] != self.agent_id
            else COUNTERARGUMENT_ANALYST_ID
        )
        message_id = str(uuid5(NAMESPACE_URL, f"{request_id}:request-message"))
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=review_response_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id=execution_receiver,
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Execute an audited Deliberation internal Revision",
                    payload=request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="deliberation.manager",
                        previous_stage=QUALITY_REVIEWER_ID,
                        next_stage=execution_receiver,
                    ),
                    routing=PMPRouting(
                        revision_target=execution_receiver,
                        reply_required=True,
                    ),
                    metadata=PMPMetadata(
                        created_at=review_message.metadata.updated_at,
                        updated_at=review_message.metadata.updated_at,
                        status=MessageStatus.REVISION_REQUIRED,
                        extensions={"role_definition": state.role_definition_usage[-1]},
                    ),
                ).model_dump(mode="json"),
                "message_id": message_id,
            }
        )
        self.revision_exchange.create_internal_request_once(message)
        if not any(item.message_id == message.message_id for item in state.message_history):
            state.message_history.append(message)
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": RevisionControlPhase.AUTHORIZATION_REQUIRED.value,
                "revision_epoch": revision_epoch,
                "active_request_id": request_id,
                "active_request_message_id": message.message_id,
                "active_result_id": None,
                "root_revision_request_id": request.root_revision_request_id,
                "parent_revision_request_id": request.parent_revision_request_id,
                "pending_request_ids": list(
                    dict.fromkeys([*state.revision_control.pending_request_ids, request_id])
                ),
            }
        )
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"request_written_{revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=message.message_id,
                artifact_ids=[final_id, report_id],
                reason=review.reason,
                created_at=review_message.metadata.updated_at,
            ),
        )
        self.repository.save(state)
        return request

    def _activate_pending_conclusion_revision(
        self,
        state: DeliberationWorkflowState,
    ) -> DeliberationWorkflowState:
        pending: list[tuple[PMPMessage, RevisionRequestV1]] = []
        for message in self.revision_exchange.list_requests(
            target_layer=LayerId.DELIBERATION,
            workflow_id=state.workflow_id,
        ):
            request = self.revision_exchange.validator.validate_request_message(
                message
            )
            if (
                request.route == RevisionRoute.UPSTREAM.value
                and request.source_layer == LayerId.CONCLUSION.value
                and request.revision_request_id
                not in state.revision_control.consumed_request_ids
            ):
                pending.append((message, request))
        if not pending:
            raise FileNotFoundError(
                f"No pending Conclusion Revision Request exists for {state.workflow_id}"
            )
        if len(pending) > 1:
            raise ValueError("Multiple pending Conclusion Revision Requests require review")
        message, request = pending[0]
        allowed_targets = {
            *PRIMARY_ANALYST_IDS,
            COUNTERARGUMENT_ANALYST_ID,
            self.agent_id,
        }
        if set(request.target_agent_ids) - allowed_targets:
            raise ValueError("Conclusion requested an invalid Deliberation target")
        if request.evidence_expansion_allowed or request.retrieval_allowed:
            raise ValueError(
                "Conclusion cannot authorize new evidence inside Deliberation"
            )
        owned = next(
            (
                item
                for item in request.base_artifacts
                if item.artifact_type == "deliberation.deliberation_result"
            ),
            None,
        )
        if state.deliberation_result is None or owned is None:
            raise ValueError("Conclusion Revision lacks Deliberation Result provenance")
        if (
            owned.artifact_id
            != str(state.deliberation_result.get("deliberation_result_id") or "")
            or owned.sha256 != canonical_sha256(state.deliberation_result)
        ):
            raise ValueError("Conclusion Revision Request is stale for Deliberation Result")
        if not any(item.message_id == message.message_id for item in state.message_history):
            state.message_history.append(message)
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": RevisionControlPhase.AUTHORIZATION_REQUIRED.value,
                "revision_epoch": request.revision_epoch,
                "active_request_id": request.revision_request_id,
                "active_request_message_id": message.message_id,
                "active_result_id": None,
                "root_revision_request_id": request.root_revision_request_id,
                "parent_revision_request_id": request.parent_revision_request_id,
                "pending_request_ids": list(
                    dict.fromkeys(
                        [
                            *state.revision_control.pending_request_ids,
                            request.revision_request_id,
                        ]
                    )
                ),
            }
        )
        state.status = WorkflowStatus.BLOCKED
        state.completed_at = None
        state.error = {
            "code": "DOWNSTREAM_REVISION_AUTHORIZATION_REQUIRED",
            "message": "Conclusion requested a Deliberation Revision; authorization required",
        }
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"downstream_request_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.REQUEST_CONSUMED,
                actor_id=self.agent_id,
                message_id=message.message_id,
                reason="Deliberation adopted the Conclusion request at the authorization boundary",
            ),
        )
        self.repository.save(state)
        return state

    def _active_revision_request_message(
        self,
        state: DeliberationWorkflowState,
    ) -> PMPMessage:
        request_id = state.revision_control.active_request_id
        message_id = state.revision_control.active_request_message_id
        if not request_id or not message_id:
            raise ValueError("Deliberation has no active Revision Request")
        message = next(
            (item for item in state.message_history if item.message_id == message_id),
            None,
        )
        if message is None:
            try:
                message = self.revision_exchange.load_internal_request(
                    layer=LayerId.DELIBERATION,
                    workflow_id=state.workflow_id,
                    revision_request_id=request_id,
                )
            except FileNotFoundError:
                try:
                    message = self.revision_exchange.load_request(
                        target_layer=LayerId.DELIBERATION,
                        workflow_id=state.workflow_id,
                        revision_request_id=request_id,
                    )
                except FileNotFoundError:
                    message = self.revision_exchange.load_request(
                        target_layer=LayerId.RESEARCHER,
                        workflow_id=state.workflow_id,
                        revision_request_id=request_id,
                    )
        request = self.revision_exchange.validator.validate_request_message(message)
        if request.revision_request_id != request_id:
            raise ValueError("Deliberation active Revision identity is inconsistent")
        return message

    def _create_revision_authorization(
        self,
        state: DeliberationWorkflowState,
        request: RevisionRequestV1,
        *,
        actor_id: str,
        actor_source: str,
        reason: str,
    ) -> RevisionExecutionAuthorization:
        rerun_stages = self._revision_stages(request.target_agent_ids)
        max_calls = len(
            [item for item in request.target_agent_ids if item in PRIMARY_ANALYST_IDS]
        ) + sum(
            stage in rerun_stages
            for stage in (
                "initial_integration",
                "counterargument",
                "final_integration",
                "quality_review",
            )
        )
        try:
            existing = self.revision_exchange.load_authorization(
                executing_layer=LayerId.DELIBERATION,
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.actor_id != actor_id
                or existing.actor_source != actor_source
                or existing.reason != reason
            ):
                raise ValueError("Deliberation Revision was authorized by another actor")
            return existing
        authorization = RevisionExecutionAuthorization(
            authorization_id=(
                "revision_authorization_"
                + uuid5(NAMESPACE_URL, request.revision_request_id).hex
            ),
            workflow_id=state.workflow_id,
            revision_request_id=request.revision_request_id,
            executing_layer=LayerId.DELIBERATION,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            max_provider_calls=max_calls,
            max_retrieval_calls=0,
        )
        self.revision_exchange.create_authorization_once(authorization)
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"authorization_created_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.AUTHORIZATION_CREATED,
                actor_id=actor_id,
                reason=reason,
                created_at=authorization.created_at,
            ),
        )
        return authorization

    def _revision_provider_task_ids(
        self,
        state: DeliberationWorkflowState,
        request: RevisionRequestV1,
    ) -> list[str]:
        rerun_stages = self._revision_stages(request.target_agent_ids)
        task_ids = [
            self._revision_task_id(state, target)
            for target in request.target_agent_ids
            if target in PRIMARY_ANALYST_IDS
        ]
        if "initial_integration" in rerun_stages:
            task_ids.append(
                self._manager_integration_task_id(
                    state, "initial_integration", recovery=False
                )
            )
        if "counterargument" in rerun_stages:
            task_ids.append(self._counterargument_task_id(state))
        if "final_integration" in rerun_stages:
            task_ids.append(
                self._manager_integration_task_id(
                    state, "final_integration", recovery=False
                )
            )
        if "quality_review" in rerun_stages:
            task_ids.append(f"delib_review_task_revision_{state.revision_count}")
        return list(dict.fromkeys(task_ids))

    async def revise(
        self,
        workflow_id: str,
        *,
        actor_id: str = "cli.operator",
        actor_source: str = "CLI",
        reason: str = "Operator authorized one Deliberation internal Revision cycle",
        progress_callback: ProgressCallback | None = None,
    ) -> DeliberationWorkflowState:
        state = self.repository.load(workflow_id)
        if state.revision_control.phase != RevisionControlPhase.AUTHORIZATION_REQUIRED.value:
            try:
                state = self._activate_pending_conclusion_revision(state)
            except FileNotFoundError:
                if (
                    state.status == WorkflowStatus.COMPLETED.value
                    and state.revision_control.phase
                    == RevisionControlPhase.COMPLETED.value
                ):
                    return state
                raise
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        authorization = self._create_revision_authorization(
            state,
            request,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
        )
        self.repository.save(state)
        outcome, rerun_initial, rerun_counterargument = (
            await self._execute_authorized_internal_revision(
                state,
                request=request,
                authorization=authorization,
                progress_callback=progress_callback,
            )
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

    async def _execute_authorized_internal_revision(
        self,
        state: DeliberationWorkflowState,
        *,
        request: RevisionRequestV1,
        authorization: RevisionExecutionAuthorization,
        progress_callback: ProgressCallback | None,
    ) -> tuple[DeliberationWorkflowState | None, bool, bool]:
        current_hashes = {
                (
                    "deliberation.final_integration",
                    str((state.final_integration or {}).get("integration_id") or ""),
                ): canonical_sha256(state.final_integration),
                (
                    "researcher.research_report",
                    str(state.research_report.get("research_report_id") or ""),
                ): canonical_sha256(state.research_report),
                (
                    "deliberation.deliberation_result",
                    str((state.deliberation_result or {}).get("deliberation_result_id") or ""),
                ): canonical_sha256(state.deliberation_result),
            }
        owned_artifacts = [
            item
            for item in request.base_artifacts
            if item.artifact_type.startswith("deliberation.")
            or item.artifact_type == "researcher.research_report"
        ]
        self.revision_exchange.validator.validate_current_base_artifacts(
            request.model_copy(update={"base_artifacts": owned_artifacts}),
            current_hashes,
        )
        try:
            if request.route == RevisionRoute.INTERNAL.value:
                budget = self.revision_exchange.budget_store.consume(
                    policy=RevisionBudgetPolicy(
                        internal_limit=max(0, self.max_revisions - 1),
                        upstream_limit=self.max_revisions,
                    ),
                    workflow_id=state.workflow_id,
                    layer=LayerId.DELIBERATION,
                    route=RevisionRoute.INTERNAL,
                    revision_request_id=request.revision_request_id,
                )
            else:
                budget = self.revision_exchange.budget_store.for_request(
                    workflow_id=state.workflow_id,
                    layer=LayerId.CONCLUSION,
                    route=RevisionRoute.UPSTREAM,
                    revision_request_id=request.revision_request_id,
                )
                if budget is None:
                    raise RevisionBudgetExhausted(
                        "Conclusion upstream Revision has no consumed request budget"
                    )
                if state.revision_count >= self.max_revisions:
                    raise RevisionBudgetExhausted(
                        "Deliberation internal execution budget is exhausted"
                    )
        except RevisionBudgetExhausted as exc:
            state.revision_count = max(state.revision_count, self.max_revisions)
            if not any(
                item.iteration == state.revision_count
                for item in state.revision_history
            ):
                pending_review = self._pending_revision_review(state)
                state.revision_history.append(
                    DeliberationRevisionRecord(
                        iteration=state.revision_count,
                        target_agent_ids=request.target_agent_ids,
                        findings=[
                            item.model_dump(mode="json")
                            for item in pending_review.findings
                        ],
                        rerun_stages=self._revision_stages(
                            request.target_agent_ids
                        ),
                    )
                )
            state.revision_control.phase = RevisionControlPhase.BLOCKED
            return (
                await self._block(
                    state,
                    f"Quality Reviewerが{self.max_revisions}回revision_requiredを返したため停止しました",
                    progress_callback,
                ),
                False,
                False,
            )
        if request.route == RevisionRoute.INTERNAL.value:
            state.revision_count = max(state.revision_count, budget.iteration)
        else:
            state.revision_count += 1
        provider_ids = self._revision_provider_task_ids(state, request)
        consumed = self.revision_exchange.consume_authorization(
            authorization,
            provider_reservation_ids=provider_ids,
            retrieval_reservation_ids=[],
        )
        state.revision_control.phase = RevisionControlPhase.EXECUTING
        state.status = WorkflowStatus.REVISING
        state.error = None
        state.completed_at = None
        for event in (
            RevisionAuditEvent(
                audit_event_id=f"authorization_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.AUTHORIZATION_CONSUMED,
                actor_id=authorization.actor_id,
                reservation_ids=provider_ids,
                reason=authorization.reason,
                created_at=consumed.consumed_at,
            ),
            RevisionAuditEvent(
                audit_event_id=f"budget_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.BUDGET_CONSUMED,
                actor_id=self.agent_id,
                reason=f"Deliberation internal revision slot {budget.iteration}",
                created_at=budget.consumed_at,
            ),
        ):
            self._record_revision_audit(state, event)
        self.repository.save(state)
        if request.route == RevisionRoute.UPSTREAM.value:
            existing_review = DeliberationQualityReviewOutput.model_validate(
                state.review_result
            )
            synthetic_findings = [
                QualityFinding(
                    finding_id=finding_id,
                    severity="MAJOR",
                    category="downstream_revision_request",
                    issue="Conclusion requested additional Deliberation analysis",
                    required_action=request.required_actions[
                        min(index, len(request.required_actions) - 1)
                    ],
                    affected_agent_ids=request.target_agent_ids,
                    evidence_ids=[],
                )
                for index, finding_id in enumerate(request.source_finding_ids)
            ]
            review = existing_review.model_copy(
                update={
                    "review_id": request.source_review_id,
                    "status": QualityGateDecision.REVISION_REQUIRED.value,
                    "conclusion_readiness": ConclusionReadiness.NOT_READY.value,
                    "reason": request.required_actions[0],
                    "findings": synthetic_findings,
                    "blocking_finding_ids": request.source_finding_ids,
                    "revision_scope": (
                        RevisionScope.TARGETED.value
                        if len(request.target_agent_ids) == 1
                        else RevisionScope.MULTI_AGENT.value
                    ),
                    "revision_targets": request.target_agent_ids,
                    "upstream_revision_requests": [],
                }
            )
        elif state.review_result is None:
            request_message = self._active_revision_request_message(state)
            review_message = next(
                (
                    item
                    for item in state.message_history
                    if item.message_id == request_message.parent_message_id
                ),
                None,
            )
            if review_message is None:
                raise ValueError(
                    "Deliberation Revision cannot recover its source Quality Review"
                )
            recovered_review = DeliberationQualityReviewOutput.model_validate(
                review_message.payload
            )
            state.review_result = recovered_review.model_dump(mode="json")
            self._store_pending_revision(state, recovered_review)
            review = recovered_review
        else:
            review = self._pending_revision_review(state)
        return await self._start_internal_revision(
            state,
            review,
            targets=request.target_agent_ids,
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
        common_execution = (
            state.revision_control.phase == RevisionControlPhase.EXECUTING.value
        )
        if not common_execution:
            state.revision_count += 1
        rerun_stages = self._revision_stages(targets)
        if not any(
            item.iteration == state.revision_count for item in state.revision_history
        ):
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
        if not common_execution and state.revision_count >= self.max_revisions:
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
            self._replace_analysis_tasks(state, revision_tasks)
            self.repository.save(state)
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
            task_id=self._counterargument_task_id(state),
            initial_integration_id=initial.integration_id,
            key_claim_ids=key_claim_ids,
            candidate_viewpoint_ids=[item.viewpoint_id for item in initial.candidate_viewpoints],
            evidence_ids=[item.evidence_id for item in report.evidence_items],
            initial_integration=initial.model_dump(mode="json"),
            research_report=self._research_context_from_state(state, report),
            revision_context=self._latest_revision_context(state) if is_revision else None,
        )
        recovered = self._recover_saved_counterargument_result(state, task, initial)
        if recovered is not None:
            return recovered
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

    def _recover_saved_counterargument_result(
        self,
        state: DeliberationWorkflowState,
        task: CounterargumentTask,
        initial: InitialIntegratedAnalysis,
    ) -> CounterargumentAnalysisResult | None:
        requests = [
            message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == COUNTERARGUMENT_ANALYST_ID
            and message.message_type == MessageType.DELIBERATION_TASK_ASSIGNMENT.value
            and message.payload.get("task_id") == task.task_id
        ]
        for request in reversed(requests):
            for response in reversed(state.message_history):
                if (
                    response.parent_message_id != request.message_id
                    or response.sender_agent_id != COUNTERARGUMENT_ANALYST_ID
                    or response.receiver_agent_id != self.agent_id
                ):
                    continue
                payload = (
                    response.payload.get("invalid_payload")
                    if response.message_type == MessageType.ERROR.value
                    else response.payload
                    if response.message_type
                    == MessageType.DELIBERATION_TASK_RESULT.value
                    else None
                )
                if not isinstance(payload, dict):
                    continue
                normalized, recovery_audit = normalize_saved_counterargument_payload(
                    payload
                )
                try:
                    result = CounterargumentAnalysisResult.model_validate(normalized)
                except Exception:
                    continue
                if result.task_id != task.task_id:
                    continue
                if result.analysis_id in {result.task_id, initial.integration_id}:
                    continue
                if result.unrouted_required_counterargument_ids():
                    continue
                unknown = self._collect_evidence_ids(normalized) - set(task.evidence_ids)
                if unknown:
                    continue
                changed = (
                    recovery_audit["analysis_id_before"]
                    != recovery_audit["analysis_id_after"]
                    or bool(recovery_audit["removed_revision_target_agent_ids"])
                )
                if changed and not any(
                    item.get("source_message_id") == response.message_id
                    for item in state.counterargument_payload_recoveries
                ):
                    state.counterargument_payload_recoveries.append(
                        {
                            "task_id": task.task_id,
                            "source_message_id": response.message_id,
                            "recovered_at": utc_now().isoformat(),
                            "provider_call_reused": False,
                            "compatibility_adapter": (
                                "canonical_counterargument_analysis_id+"
                                "filter_non_internal_revision_targets"
                            ),
                            **recovery_audit,
                        }
                    )
                    self.repository.save(state)
                return result
        return None

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
            agent_id: payload
            for agent_id, payload in self._normalized_primary_analyses(state).items()
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
        known_counterargument_ids = {
            item.counterargument_id for item in counterargument.counterarguments
        }
        known_challenge_ids = {
            item.challenge_id for item in counterargument.steelman_arguments
        }
        unknown_dispositions = sorted(disposition_ids - known_counterargument_ids)
        if unknown_dispositions:
            raise ValueError(
                "Final integration contains dispositions for unknown "
                f"counterarguments: {unknown_dispositions}"
            )
        unknown_change_sources = sorted(
            {
                identifier
                for change in final.integration_changes
                for identifier in change.source_counterargument_ids
            }
            - known_counterargument_ids
        )
        if unknown_change_sources:
            raise ValueError(
                "Final integration changes reference unknown counterarguments: "
                f"{unknown_change_sources}"
            )
        for index, entry in enumerate(final.traceability_index):
            unknown_counterarguments = sorted(
                set(entry.counterargument_ids) - known_counterargument_ids
            )
            if unknown_counterarguments:
                raise ValueError(
                    f"Final traceability[{index}] references unknown "
                    f"counterarguments: {unknown_counterarguments}"
                )
            unknown_challenges = sorted(
                set(entry.challenge_ids) - known_challenge_ids
            )
            if unknown_challenges:
                raise ValueError(
                    f"Final traceability[{index}] references unknown challenges: "
                    f"{unknown_challenges}"
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
    def _select_pmp_routing_messages(
        state: DeliberationWorkflowState,
        review_request: PMPMessage,
    ) -> list[PMPMessage]:
        """Select the current auditable route without serializing all history.

        Full PMP history remains persisted. The reviewer needs the current
        Researcher handoff, current primary task pairs, Counterargument failure /
        repair pair, and at most the latest two review attempts.
        """

        history = list(state.message_history)
        selected_ids: set[str] = set()

        researcher_messages = [
            item
            for item in history
            if item.sender_agent_id == "researcher.manager"
            and item.receiver_agent_id == "deliberation.manager"
        ]
        if researcher_messages:
            selected_ids.add(researcher_messages[-1].message_id)

        current_primary_task_ids = {
            payload.get("task_id")
            for agent_id, payload in state.analysis_results.items()
            if agent_id in PRIMARY_ANALYST_IDS and payload.get("task_id")
        }
        for message in history:
            if (
                message.payload.get("task_id") in current_primary_task_ids
                and (
                    message.sender_agent_id in PRIMARY_ANALYST_IDS
                    or message.receiver_agent_id in PRIMARY_ANALYST_IDS
                )
            ):
                selected_ids.add(message.message_id)

        current_counter_prefix = f"counter_task_revision_{state.revision_count}"
        for message in history:
            task_id = str(message.payload.get("task_id") or "")
            if task_id.startswith(current_counter_prefix) and (
                message.sender_agent_id == COUNTERARGUMENT_ANALYST_ID
                or message.receiver_agent_id == COUNTERARGUMENT_ANALYST_ID
            ):
                selected_ids.add(message.message_id)

        review_assignments = [
            item
            for item in history
            if item.message_type
            == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
        ][-2:]
        review_request_ids = {item.message_id for item in review_assignments}
        for message in history:
            if (
                message.message_id in review_request_ids
                or message.parent_message_id in review_request_ids
            ):
                selected_ids.add(message.message_id)

        seen_message_ids: set[str] = set()
        selected = []
        for item in history:
            if (
                item.message_id in selected_ids
                and item.message_id not in seen_message_ids
            ):
                selected.append(item)
                seen_message_ids.add(item.message_id)
        selected.append(review_request)
        return selected

    @staticmethod
    def _build_pmp_routing_trace(
        state: DeliberationWorkflowState,
        review_request: PMPMessage,
    ) -> list[dict[str, Any]]:
        messages = DeliberationManager._select_pmp_routing_messages(
            state,
            review_request,
        )
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
        research_context: DeliberationResearchContext | None = None,
        recovery: bool = False,
        reuse_saved_response: bool = True,
        task_variant: str | None = None,
    ) -> tuple[DeliberationQualityReviewOutput, PMPMessage]:
        state.status = WorkflowStatus.REVIEWING
        base_review_task_id = f"delib_review_task_revision_{state.revision_count}"
        normal_review_task_id = (
            f"{base_review_task_id}_recovery_1" if recovery else base_review_task_id
        )
        if task_variant:
            normal_review_task_id = f"{normal_review_task_id}_{task_variant}"
        reviewer = self.registry.get(QUALITY_REVIEWER_ID)
        provider_id = getattr(reviewer.provider, "provider_id", None)
        review_error = self._latest_retryable_review_error(state)
        failed_review_task_id = (
            str(review_error.payload.get("task_id") or "")
            if review_error is not None
            else ""
        )
        authorization = None
        if isinstance(provider_id, str):
            for original_task_id in dict.fromkeys(
                [failed_review_task_id, normal_review_task_id, base_review_task_id]
            ):
                if not original_task_id:
                    continue
                authorization = self.provider_retry_store.for_original_task(
                    workflow_id=state.workflow_id,
                    provider_id=provider_id,
                    original_task_id=original_task_id,
                )
                if authorization is not None:
                    break
        review_task_id = (
            authorization.retry_task_id
            if authorization is not None
            else normal_review_task_id
        )
        effective_research_context = (
            research_context or self._research_context_from_state(state, report)
        )
        revision_context = self._latest_revision_context(state)
        expected_review_identity = {
            "research_report_id": effective_research_context.research_report_id,
            "primary_analysis_ids": sorted(
                str(payload.get("analysis_id") or "")
                for payload in primary_analyses.values()
            ),
            "initial_integration_id": initial.integration_id,
            "counterargument_analysis_id": counterargument.analysis_id,
            "final_integration_id": final.integration_id,
            "deterministic_validation": self._quality_review_validation_identity(
                validation.model_dump(mode="json")
            ),
            "revision_context": revision_context,
        }
        if authorization is None:
            prior_same_task_requests = [
                message
                for message in state.message_history
                if message.sender_agent_id == self.agent_id
                and message.receiver_agent_id == QUALITY_REVIEWER_ID
                and message.message_type
                == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
                and message.payload.get("task_id") == review_task_id
            ]
            if any(
                self._quality_review_artifact_identity(message.payload)
                != expected_review_identity
                for message in prior_same_task_requests
            ):
                identity_digest = hashlib.sha256(
                    json.dumps(
                        expected_review_identity,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()[:16]
                review_task_id = (
                    f"{normal_review_task_id}_artifact_{identity_digest}"
                )
        if reuse_saved_response:
            for saved_task_id in dict.fromkeys(
                [
                    review_task_id,
                    failed_review_task_id,
                    normal_review_task_id,
                    base_review_task_id,
                ]
            ):
                if not saved_task_id:
                    continue
                saved_exchange = self._saved_quality_review_exchange(
                    state,
                    saved_task_id,
                    expected_identity=expected_review_identity,
                )
                if saved_exchange is not None:
                    return saved_exchange
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
            research_report=effective_research_context,
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
            revision_context=revision_context,
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

    def _saved_quality_review_exchange(
        self,
        state: DeliberationWorkflowState,
        review_task_id: str,
        *,
        expected_identity: dict[str, Any],
    ) -> tuple[DeliberationQualityReviewOutput, PMPMessage] | None:
        requests = [
            message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == QUALITY_REVIEWER_ID
            and message.message_type
            == MessageType.DELIBERATION_QUALITY_REVIEW_ASSIGNMENT.value
            and message.payload.get("task_id") == review_task_id
        ]
        for request in reversed(requests):
            if self._quality_review_artifact_identity(request.payload) != expected_identity:
                continue
            for response in reversed(state.message_history):
                if (
                    response.parent_message_id != request.message_id
                    or response.sender_agent_id != QUALITY_REVIEWER_ID
                    or response.receiver_agent_id != self.agent_id
                    or response.message_type
                    != MessageType.DELIBERATION_QUALITY_REVIEW_RESULT.value
                ):
                    continue
                error = self._validate_response_envelope(
                    request,
                    response,
                    sender_agent_id=QUALITY_REVIEWER_ID,
                    expected_type=(
                        MessageType.DELIBERATION_QUALITY_REVIEW_RESULT.value
                    ),
                )
                if error is not None:
                    continue
                try:
                    review = DeliberationQualityReviewOutput.model_validate(
                        response.payload
                    )
                except Exception:
                    continue
                return review, response
        return None

    @staticmethod
    def _quality_review_artifact_identity(payload: dict[str, Any]) -> dict[str, Any]:
        primary = payload.get("primary_analyses")
        primary_ids = (
            sorted(
                str(item.get("analysis_id") or "")
                for item in primary.values()
                if isinstance(item, dict)
            )
            if isinstance(primary, dict)
            else []
        )
        report = payload.get("research_report")
        initial = payload.get("initial_integration")
        counterargument = payload.get("counterargument_analysis")
        final = payload.get("final_integration")
        return {
            "research_report_id": (
                str(report.get("research_report_id") or "")
                if isinstance(report, dict)
                else ""
            ),
            "primary_analysis_ids": primary_ids,
            "initial_integration_id": (
                str(initial.get("integration_id") or "")
                if isinstance(initial, dict)
                else ""
            ),
            "counterargument_analysis_id": (
                str(counterargument.get("analysis_id") or "")
                if isinstance(counterargument, dict)
                else ""
            ),
            "final_integration_id": (
                str(final.get("integration_id") or "")
                if isinstance(final, dict)
                else ""
            ),
            "deterministic_validation": (
                DeliberationManager._quality_review_validation_identity(
                    payload.get("deterministic_validation")
                )
            ),
            "revision_context": payload.get("revision_context"),
        }

    @staticmethod
    def _quality_review_validation_identity(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        findings = payload.get("findings")
        normalized_findings = (
            sorted(
                (
                    str(item.get("severity") or ""),
                    str(item.get("category") or ""),
                    str(item.get("message") or ""),
                    tuple(sorted(str(value) for value in item.get("affected_ids") or [])),
                )
                for item in findings
                if isinstance(item, dict)
            )
            if isinstance(findings, list)
            else []
        )
        return {
            "schema_version": payload.get("schema_version"),
            "passed": payload.get("passed"),
            "findings": normalized_findings,
            "metrics": payload.get("metrics"),
            "validation_targets": payload.get("validation_targets"),
        }

    def _finalize_internal_revision(
        self,
        state: DeliberationWorkflowState,
        *,
        review_response_id: str,
        completed: bool,
        reason: str,
    ) -> None:
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        review_message = next(
            (
                item
                for item in state.message_history
                if item.message_id == review_response_id
            ),
            None,
        )
        if review_message is None:
            raise ValueError("Deliberation Revision result has no Quality Review PMP")
        result_artifacts: list[RevisionArtifactRef] = []
        if state.final_integration is not None:
            final_id = str(state.final_integration.get("integration_id") or "")
            if final_id:
                result_artifacts.append(
                    RevisionArtifactRef(
                        artifact_type="deliberation.final_integration",
                        artifact_id=final_id,
                        sha256=canonical_sha256(state.final_integration),
                    )
                )
        if state.deliberation_result is not None:
            result_id = str(
                state.deliberation_result.get("deliberation_result_id") or ""
            )
            if result_id:
                result_artifacts.append(
                    RevisionArtifactRef(
                        artifact_type="deliberation.deliberation_result",
                        artifact_id=result_id,
                        sha256=canonical_sha256(state.deliberation_result),
                    )
                )
        authorization = self.revision_exchange.load_authorization(
            executing_layer=LayerId.DELIBERATION,
            workflow_id=state.workflow_id,
            revision_request_id=request.revision_request_id,
        )
        provider_ids = list(authorization.provider_reservation_ids)
        result_id = "revision_result_" + uuid5(
            NAMESPACE_URL, f"{request.revision_request_id}:result"
        ).hex
        result = RevisionResultV1.create(
            revision_result_id=result_id,
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=state.workflow_id,
            requester_layer=request.source_layer,
            producer_layer=request.target_layer,
            revision_epoch=request.revision_epoch,
            status=(
                RevisionExecutionStatus.COMPLETED
                if completed
                else RevisionExecutionStatus.PARTIAL
            ),
            base_artifacts=request.base_artifacts,
            result_artifacts=result_artifacts,
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id=finding_id,
                    outcome=(
                        RevisionFindingOutcome.RESOLVED
                        if completed
                        else RevisionFindingOutcome.UNRESOLVED
                    ),
                    reason=reason,
                    result_artifact_ids=[
                        item.artifact_id for item in result_artifacts
                    ],
                )
                for finding_id in request.source_finding_ids
            ],
            human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            provider_reservation_ids=provider_ids,
            retrieval_reservation_ids=[],
            provider_call_count=len(provider_ids),
            retrieval_call_count=0,
            completed_at=review_message.metadata.updated_at,
        )
        result_message_id = str(
            uuid5(NAMESPACE_URL, f"{request.revision_request_id}:result-message")
        )
        result_message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=request_message.message_id,
                    sender_agent_id=request_message.receiver_agent_id,
                    receiver_agent_id=request_message.sender_agent_id,
                    message_type=MessageType.REVISION_RESULT,
                    objective=f"Return the audited Deliberation {request.route} Revision result",
                    payload=result.model_dump(mode="json"),
                    routing=PMPRouting(revision_target=None, reply_required=False),
                    metadata=PMPMetadata(
                        created_at=review_message.metadata.updated_at,
                        updated_at=review_message.metadata.updated_at,
                        status=(
                            MessageStatus.COMPLETED
                            if completed
                            else MessageStatus.REVISION_REQUIRED
                        ),
                    ),
                ).model_dump(mode="json"),
                "message_id": result_message_id,
            }
        )
        if request.route == RevisionRoute.INTERNAL.value:
            self.revision_exchange.create_internal_result_once(
                request_message,
                result_message,
            )
        else:
            self.revision_exchange.create_result_once(
                request_message,
                result_message,
            )
        if not any(
            item.message_id == result_message.message_id
            for item in state.message_history
        ):
            state.message_history.append(result_message)
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"result_written_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.RESULT_WRITTEN,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[item.artifact_id for item in result_artifacts],
                reservation_ids=provider_ids,
                reason=reason,
                created_at=review_message.metadata.updated_at,
            ),
        )
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": RevisionControlPhase.COMPLETED.value,
                "active_result_id": result_id,
                "pending_request_ids": [
                    item
                    for item in state.revision_control.pending_request_ids
                    if item != request.revision_request_id
                ],
                "consumed_request_ids": list(
                    dict.fromkeys(
                        [
                            *state.revision_control.consumed_request_ids,
                            request.revision_request_id,
                        ]
                    )
                ),
                "consumed_result_ids": list(
                    dict.fromkeys(
                        [*state.revision_control.consumed_result_ids, result_id]
                    )
                ),
            }
        )
        self._clear_pending_revision(state)

    def _record_revision_audit(
        self,
        state: DeliberationWorkflowState,
        event: RevisionAuditEvent,
    ) -> None:
        self.revision_exchange.create_audit_event_once(event)
        if event.audit_event_id not in state.revision_control.audit_event_ids:
            state.revision_control.audit_event_ids.append(event.audit_event_id)

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
        source_finding_ids = list(
            dict.fromkeys(
                finding_id
                for item in review.upstream_revision_requests
                for finding_id in item.source_finding_ids
            )
        )
        revision_epoch = max(
            state.revision_count,
            state.revision_control.revision_epoch,
        ) + 1
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.DELIBERATION,
            target_layer=LayerId.RESEARCHER,
            revision_epoch=revision_epoch,
            source_review_id=parent_message_id,
            source_finding_ids=source_finding_ids,
        )
        parent_request_id = (
            state.revision_control.active_request_id
            if state.revision_control.active_request_id
            and state.revision_control.phase == RevisionControlPhase.COMPLETED.value
            else None
        )
        root_request_id = (
            (state.revision_control.root_revision_request_id or parent_request_id)
            if parent_request_id
            else request_id
        )
        canonical_request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.UPSTREAM,
            source_layer=LayerId.DELIBERATION,
            target_layer=LayerId.RESEARCHER,
            revision_epoch=revision_epoch,
            root_revision_request_id=root_request_id,
            parent_revision_request_id=parent_request_id,
            source_review_id=parent_message_id,
            source_finding_ids=source_finding_ids,
            target_agent_ids=["researcher.manager"],
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="researcher.research_report",
                    artifact_id=str(state.research_report["research_report_id"]),
                    sha256=canonical_sha256(state.research_report),
                ),
                RevisionArtifactRef(
                    artifact_type="deliberation.final_integration",
                    artifact_id=str(state.final_integration["integration_id"]),
                    sha256=canonical_sha256(state.final_integration),
                ),
            ],
            required_actions=list(
                dict.fromkeys(
                    item.missing_evidence_description
                    for item in review.upstream_revision_requests
                )
            ),
            acceptance_conditions=list(
                dict.fromkeys(
                    condition
                    for item in review.upstream_revision_requests
                    for condition in item.acceptance_conditions
                )
            ),
            evidence_expansion_allowed=True,
            retrieval_allowed=True,
            expected_human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
        )
        canonical_message_id = str(
            uuid5(NAMESPACE_URL, f"{request_id}:request-message")
        )
        canonical_message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=parent_message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id="researcher.manager",
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Collect evidence through an audited upstream Revision",
                    payload=canonical_request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="deliberation.upstream_revision",
                        previous_stage="deliberation.quality_review",
                        next_stage="researcher.manager",
                    ),
                    routing=PMPRouting(
                        revision_target="researcher.manager",
                        reply_required=True,
                    ),
                    metadata=PMPMetadata(
                        status=MessageStatus.REVISION_REQUIRED,
                        extensions={"role_definition": state.role_definition_usage[-1]},
                    ),
                ).model_dump(mode="json"),
                "message_id": canonical_message_id,
            }
        )
        self.revision_exchange.create_request_once(
            canonical_message,
            budget_policy=RevisionBudgetPolicy(
                internal_limit=max(0, self.max_revisions - 1),
                upstream_limit=self.max_revisions,
            ),
        )
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
        for saved_message in (canonical_message, message):
            if not any(
                item.message_id == saved_message.message_id
                for item in state.message_history
            ):
                state.message_history.append(saved_message)
        state.upstream_revision_count += 1
        state.upstream_revision_history.append(
            UpstreamRevisionRecord(
                iteration=state.upstream_revision_count,
                request_message_id=message.message_id,
                requests=[item.model_dump(mode="json") for item in review.upstream_revision_requests],
            )
        )
        state.status = WorkflowStatus.WAITING_UPSTREAM_REVISION
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": RevisionControlPhase.WAITING_UPSTREAM_RESULT.value,
                "revision_epoch": revision_epoch,
                "active_request_id": request_id,
                "active_request_message_id": canonical_message.message_id,
                "active_result_id": None,
                "root_revision_request_id": root_request_id,
                "parent_revision_request_id": parent_request_id,
                "pending_request_ids": list(
                    dict.fromkeys([*state.revision_control.pending_request_ids, request_id])
                ),
            }
        )
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"upstream_request_written_{revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.DELIBERATION,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=canonical_message.message_id,
                artifact_ids=[
                    str(state.research_report["research_report_id"]),
                    str(state.final_integration["integration_id"]),
                ],
                reason=review.reason,
            ),
        )
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
            data["task_id"] = self._revision_task_id(state, target)
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

    @staticmethod
    def _revision_task_id(
        state: DeliberationWorkflowState,
        target_agent_id: str,
    ) -> str:
        identity = (
            f"{state.workflow_id}|revision={state.revision_count}|{target_agent_id}"
        )
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        role = target_agent_id.rsplit(".", 1)[-1]
        return f"delib_task_revision_{state.revision_count}_{role}_{digest}"

    def _manager_integration_task_id(
        self,
        state: DeliberationWorkflowState,
        stage: str,
        *,
        recovery: bool,
    ) -> str:
        suffix = "_recovery_1" if recovery else ""
        base = f"deliberation_manager_{stage}_revision_{state.revision_count}{suffix}"
        provider_id = getattr(self.registry.provider, "provider_id", None)
        current_failure = self._current_manager_provider_failure(state)
        if (
            current_failure is not None
            and current_failure.get("stage") == stage
            and isinstance(provider_id, str)
        ):
            repair = self.provider_contract_repair_store.for_original_task(
                workflow_id=state.workflow_id,
                provider_id=provider_id,
                original_task_id=current_failure["logical_task_id"],
            )
            if (
                repair is not None
                and repair.status == ProviderContractRepairStatus.PENDING.value
            ):
                return repair.repair_task_id
            failure_authorization = self.provider_retry_store.for_original_task(
                workflow_id=state.workflow_id,
                provider_id=provider_id,
                original_task_id=current_failure["logical_task_id"],
            )
            if failure_authorization is not None:
                return failure_authorization.retry_task_id
        if not recovery:
            selected = base
        else:
            contract_repair = f"{base}_contract_repair_1"
            if contract_repair in state.manager_invalid_payloads:
                selected = contract_repair
            else:
                error_message = str((state.error or {}).get("message") or "")
                schema_name = {
                    "initial_integration": "InitialIntegratedAnalysis",
                    "final_integration": "FinalIntegratedAnalysis",
                }.get(stage, "")
                has_saved_validation_failure = any(
                    record.get("stage") == stage
                    for record in state.manager_invalid_payloads.values()
                )
                selected = (
                    contract_repair
                    if has_saved_validation_failure
                    or (
                        schema_name
                        and f"validation error for {schema_name}" in error_message
                    )
                    else base
                )
        authorization = (
            self.provider_retry_store.for_original_task(
                workflow_id=state.workflow_id,
                provider_id=provider_id,
                original_task_id=selected,
            )
            if isinstance(provider_id, str)
            else None
        )
        return authorization.retry_task_id if authorization is not None else selected

    @staticmethod
    def _counterargument_task_id(state: DeliberationWorkflowState) -> str:
        base = f"counter_task_revision_{state.revision_count}"
        repair = f"{base}_context_repair_1"
        for message in state.message_history:
            if message.payload.get("task_id") == repair:
                return repair
        for message in state.message_history:
            if (
                message.message_type == MessageType.ERROR.value
                and message.payload.get("task_id") == base
            ):
                detail = str(message.payload.get("message") or "").lower()
                if any(
                    marker in detail
                    for marker in (
                        "maximum context length",
                        "context length is",
                        "reduce the length",
                        "context budget",
                    )
                ):
                    return repair
        return base

    def _build_result(
        self,
        state: DeliberationWorkflowState,
        report: ResearchReport,
        final: FinalIntegratedAnalysis,
        counterargument: CounterargumentAnalysisResult,
        review: DeliberationQualityReviewOutput | None,
    ) -> DeliberationResult:
        human_context = self._research_context_from_state(state, report)
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
            + [
                "Human-accepted unresolved evidence gap "
                f"{item.finding_id}: {item.issue}"
                for item in human_context.accepted_evidence_gaps
            ]
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
            human_evidence_decision=human_context.human_evidence_decision,
            accepted_evidence_gaps=human_context.accepted_evidence_gaps,
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
            "human_evidence_decision",
            "accepted_evidence_gaps",
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

    @classmethod
    def _optional_analysis(cls, state, agent_id: str, schema):
        payload = state.analysis_results.get(agent_id)
        if not payload:
            return None
        return schema.model_validate(
            cls._normalize_saved_analysis_payload(agent_id, payload)
        )

    @staticmethod
    def _collect_evidence_ids(value: Any) -> set[str]:
        return DeliberationValidator._collect_evidence_ids(value)

    @staticmethod
    def _evidence_to_source_ids(
        state: DeliberationWorkflowState,
    ) -> dict[str, str]:
        report = ResearchReport.model_validate(state.research_report)
        return {item.evidence_id: item.source_id for item in report.evidence_items}

    @classmethod
    def _validate_analysis_source_bindings(
        cls,
        state: DeliberationWorkflowState,
        payload: dict[str, Any],
    ) -> str | None:
        evidence_to_source = cls._evidence_to_source_ids(state)
        known_source_ids = set(evidence_to_source.values())
        for fact in payload.get("specific_facts", []):
            if not isinstance(fact, dict):
                continue
            actual = {
                item
                for item in fact.get("source_ids", [])
                if isinstance(item, str)
            }
            unknown = actual - known_source_ids
            if unknown:
                return f"referenced sources outside its ResearchReport: {sorted(unknown)}"
            expected = {
                evidence_to_source[item]
                for item in fact.get("evidence_ids", [])
                if item in evidence_to_source
            }
            if actual != expected:
                return (
                    "specific_fact source_ids do not match its evidence_ids: "
                    f"expected {sorted(expected)}, got {sorted(actual)}"
                )
        return None

    @classmethod
    def _repair_saved_stakeholder_source_bindings(
        cls,
        state: DeliberationWorkflowState,
        agent_id: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Repair only persisted legacy/error payloads via an authoritative 1:1 map."""
        if agent_id != "deliberation.stakeholder_response_analyst":
            return payload, False
        evidence_to_source = cls._evidence_to_source_ids(state)
        normalized = dict(payload)
        facts: list[Any] = []
        repaired = False
        for raw_fact in payload.get("specific_facts", []):
            if not isinstance(raw_fact, dict):
                facts.append(raw_fact)
                continue
            fact = dict(raw_fact)
            evidence_ids = [
                item for item in fact.get("evidence_ids", []) if isinstance(item, str)
            ]
            if evidence_ids and all(item in evidence_to_source for item in evidence_ids):
                expected = list(
                    dict.fromkeys(evidence_to_source[item] for item in evidence_ids)
                )
                if fact.get("source_ids") != expected:
                    fact["source_ids"] = expected
                    repaired = True
            facts.append(fact)
        normalized["specific_facts"] = facts
        return normalized, repaired

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
