from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

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
from common.provider_contract_repair import (
    PROVIDER_CONTRACT_REPAIR_SUFFIX,
    ProviderContractRepairAuthorization,
    ProviderContractRepairAuthorizationStore,
)
from common.provider_model_compatibility import ProviderModelCompatibilityStore
from common.provider_retry import (
    OPERATOR_RETRY_SUFFIX,
    ProviderRetryAuthorization,
    ProviderRetryAuthorizationStore,
)
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
    CandidateCoverageAudit,
    ConclusionManagerRepairRecord,
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
from deliberation.schemas.review import (
    ConclusionReadiness as DeliberationConclusionReadiness,
    DeliberationQualityReviewOutput,
)
from storage.conclusion_workflow_repository import ConclusionWorkflowRepository
from storage.revision_exchange_repository import (
    RevisionBudgetExhausted,
    RevisionExchangeRepository,
)


ProgressCallback = Callable[[str], Awaitable[None]]

CANDIDATE_COVERAGE_CONTRACT_REPAIR_SUFFIX = (
    "_candidate_coverage_contract_repair_1"
)


class ConclusionManager:
    agent_id = "conclusion.manager"

    def __init__(
        self,
        registry: ConclusionRegistry,
        repository: ConclusionWorkflowRepository,
        *,
        max_revisions: int = 2,
        max_manager_repairs_per_revision: int = 1,
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
        # Safe Mode blocks automatic revision at the decision boundary. Keep the
        # configured limit so an explicit operator revision can consume exactly
        # one audited cycle without enabling an automatic loop.
        self.max_revisions = max_revisions
        self.max_manager_repairs_per_revision = max_manager_repairs_per_revision
        self.rd_loader = rd_loader or registry.rd_loader
        self.pmp_validator = PMPValidator()
        self.deterministic_validator = ConclusionValidator()
        self.provider_retry_store = ProviderRetryAuthorizationStore(repository.data_dir)
        self.provider_contract_repair_store = (
            ProviderContractRepairAuthorizationStore(repository.data_dir)
        )
        self.provider_model_compatibility_store = ProviderModelCompatibilityStore(
            repository.data_dir
        )
        self.revision_exchange = RevisionExchangeRepository(repository.data_dir)

    def authorize_provider_retry(
        self,
        workflow_id: str,
    ) -> ProviderRetryAuthorization:
        """Authorize one explicit retry of the latest failed Conclusion task."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Operator provider retry is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError("Conclusion must be FAILED before an operator provider retry")
        request, error_response = self._latest_failed_provider_exchange(state)
        original_task_id = request.payload.get("task_id")
        if not isinstance(original_task_id, str) or not original_task_id:
            raise ValueError("Failed Conclusion request has no logical task_id")

        error_class = str(error_response.payload.get("error_class") or "")
        normalized_error_class = error_class
        if error_class == "PayloadValidationError" and self._is_legacy_non_finite_root_error(
            error_response
        ):
            normalized_error_class = "ProviderResponseContractError"
        elif error_class == "PayloadValidationError":
            if not self._is_persisted_provider_output_validation_error(error_response):
                raise ValueError(
                    "Conclusion PayloadValidationError has no auditable Provider output payload"
                )
        if normalized_error_class not in {
            "RetryableAgentError",
            "ProviderResponseContractError",
            "PayloadValidationError",
        }:
            raise ValueError(
                "Latest Conclusion failure is not eligible for an explicit Provider retry"
            )

        agent = self.registry.get(request.receiver_agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Conclusion Provider has no stable logical provider ID")
        return self.provider_retry_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=request.receiver_agent_id,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            source_error_class=normalized_error_class,
        )

    async def retry_provider_call(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
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
        """Authorize one distinct-model repair after original and retry fail."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Provider contract repair is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError("Conclusion must be FAILED before a provider contract repair")
        retry_request, retry_error = self._latest_failed_provider_exchange(state)
        retry_task_id = retry_request.payload.get("task_id")
        if not isinstance(retry_task_id, str) or not retry_task_id.endswith(
            OPERATOR_RETRY_SUFFIX
        ):
            raise ValueError(
                "Provider contract repair requires a failed one-shot operator retry"
            )
        if retry_error.payload.get("error_class") != "ProviderResponseContractError":
            raise ValueError(
                "Provider contract repair requires a contract-invalid retry response"
            )
        original_task_id = retry_task_id[: -len(OPERATOR_RETRY_SUFFIX)]
        original_exchange = self._failed_provider_exchange_for_task(
            state,
            original_task_id,
        )
        if original_exchange is None:
            raise ValueError(
                "Provider contract repair could not find the original failed exchange"
            )
        original_request, original_error = original_exchange
        original_error_class = str(original_error.payload.get("error_class") or "")
        if not (
            original_error_class == "ProviderResponseContractError"
            or self._is_legacy_non_finite_root_error(original_error)
        ):
            raise ValueError(
                "Provider contract repair requires two contract-invalid responses"
            )
        if original_request.receiver_agent_id != retry_request.receiver_agent_id:
            raise ValueError("Provider contract repair agent identity mismatch")

        agent = self.registry.get(retry_request.receiver_agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Conclusion Provider has no stable logical provider ID")
        failed_model_id = str(retry_error.payload.get("model_id") or "").strip()
        repair_model_id = repair_model_id.strip()
        if not failed_model_id:
            raise ValueError("Failed Conclusion response has no model identity")
        return self.provider_contract_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=retry_request.receiver_agent_id,
            original_task_id=original_task_id,
            retry_task_id=retry_task_id,
            source_error_message_id=retry_error.message_id,
            failed_model_id=failed_model_id,
            repair_model_id=repair_model_id,
        )

    async def repair_provider_contract(
        self,
        workflow_id: str,
        *,
        repair_model_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
        authorization = self.authorize_provider_contract_repair(
            workflow_id,
            repair_model_id=repair_model_id,
        )
        await self._emit(
            progress_callback,
            "One-time provider contract repair authorized: "
            + authorization.repair_task_id
            + " -> "
            + authorization.repair_model_id,
        )
        return await self.recover(
            workflow_id,
            progress_callback=progress_callback,
        )

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
        canonical_request_message: PMPMessage | None = None
        canonical_result_message: PMPMessage | None = None
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
                        "deliberation.deliberation_result",
                        str(state.deliberation_result.get("deliberation_result_id") or ""),
                    ): canonical_sha256(state.deliberation_result),
                    (
                        "conclusion.conclusion_package",
                        str((state.conclusion_package or {}).get("conclusion_package_id") or ""),
                    ): canonical_sha256(state.conclusion_package),
                },
            )
            try:
                canonical_result_message = self.revision_exchange.load_result(
                    requester_layer=LayerId.CONCLUSION,
                    workflow_id=state.workflow_id,
                    revision_request_id=canonical_request.revision_request_id,
                    request_message=canonical_request_message,
                )
            except FileNotFoundError:
                # Old Deliberation producers return only the legacy handoff.  A
                # zero-call adapter is created after that handoff is validated.
                canonical_result_message = None

        handoff = self.repository.load_deliberation_handoff(workflow_id)
        if handoff.message_id == state.deliberation_handoff.get("message_id"):
            raise ValueError("Deliberationから新しいrevision resultがまだ届いていません")
        result = self._validate_deliberation_handoff(handoff)
        if canonical_request_message is not None and canonical_result_message is None:
            canonical_result_message = self._adapt_legacy_deliberation_revision_result(
                state,
                request_message=canonical_request_message,
                handoff=handoff,
                deliberation_result=result,
            )
        manager_snapshot = self.rd_loader.load(self.agent_id)
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())
        if canonical_request_message is not None and canonical_result_message is not None:
            return await self._consume_deliberation_revision_result(
                state,
                request_message=canonical_request_message,
                result_message=canonical_result_message,
                handoff=handoff,
                deliberation_result=result,
                progress_callback=progress_callback,
            )
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

    def _active_revision_request_message(
        self,
        state: ConclusionWorkflowState,
    ) -> PMPMessage:
        request_id = state.revision_control.active_request_id
        message_id = state.revision_control.active_request_message_id
        if not request_id or not message_id:
            raise ValueError("Conclusion has no active Revision Request")
        message = next(
            (item for item in state.message_history if item.message_id == message_id),
            None,
        )
        if message is None:
            try:
                message = self.revision_exchange.load_internal_request(
                    layer=LayerId.CONCLUSION,
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
                        target_layer=LayerId.CONCLUSION,
                        workflow_id=state.workflow_id,
                        revision_request_id=request_id,
                    )
        request = self.revision_exchange.validator.validate_request_message(message)
        if request.revision_request_id != request_id:
            raise ValueError("Conclusion active Revision Request identity is inconsistent")
        return message

    def _adapt_legacy_deliberation_revision_result(
        self,
        state: ConclusionWorkflowState,
        *,
        request_message: PMPMessage,
        handoff: PMPMessage,
        deliberation_result: DeliberationResult,
    ) -> PMPMessage:
        """Wrap a validated legacy handoff in the canonical result contract."""

        request = RevisionRequestV1.model_validate(request_message.payload)
        payload = deliberation_result.model_dump(mode="json")
        deliberation_result_id = deliberation_result.deliberation_result_id
        result_id = "revision_result_" + uuid5(
            NAMESPACE_URL,
            f"{request.revision_request_id}:legacy-result",
        ).hex
        result = RevisionResultV1.create(
            revision_result_id=result_id,
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=state.workflow_id,
            requester_layer=LayerId.CONCLUSION,
            producer_layer=LayerId.DELIBERATION,
            revision_epoch=request.revision_epoch,
            status=RevisionExecutionStatus.COMPLETED,
            base_artifacts=request.base_artifacts,
            result_artifacts=[
                RevisionArtifactRef(
                    artifact_type="deliberation.deliberation_result",
                    artifact_id=deliberation_result_id,
                    sha256=canonical_sha256(payload),
                )
            ],
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id=finding_id,
                    outcome=RevisionFindingOutcome.RESOLVED,
                    reason="Validated legacy Deliberation handoff adapted without Provider calls",
                    result_artifact_ids=[deliberation_result_id],
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
            uuid5(
                NAMESPACE_URL,
                f"{request.revision_request_id}:legacy-result-message",
            )
        )
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=request_message.message_id,
                    sender_agent_id=request_message.receiver_agent_id,
                    receiver_agent_id=request_message.sender_agent_id,
                    message_type=MessageType.REVISION_RESULT,
                    objective="Adapt the validated legacy Deliberation revision handoff",
                    payload=result.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="deliberation.revision_result",
                        previous_stage="deliberation.manager",
                        next_stage="conclusion.manager",
                    ),
                    routing=PMPRouting(revision_target=None, reply_required=False),
                    metadata=PMPMetadata(
                        created_at=handoff.metadata.updated_at,
                        updated_at=handoff.metadata.updated_at,
                        status=MessageStatus.COMPLETED,
                    ),
                ).model_dump(mode="json"),
                "message_id": message_id,
            }
        )
        self.revision_exchange.create_result_once(request_message, message)
        return message

    async def _consume_deliberation_revision_result(
        self,
        state: ConclusionWorkflowState,
        *,
        request_message: PMPMessage,
        result_message: PMPMessage,
        handoff: PMPMessage,
        deliberation_result: DeliberationResult,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        request = RevisionRequestV1.model_validate(request_message.payload)
        result = RevisionResultV1.model_validate(result_message.payload)
        if result.status != RevisionExecutionStatus.COMPLETED.value:
            raise ValueError("Deliberation Revision Result is not complete")
        result_payload = deliberation_result.model_dump(mode="json")
        artifact = next(
            (
                item
                for item in result.result_artifacts
                if item.artifact_type == "deliberation.deliberation_result"
            ),
            None,
        )
        if (
            artifact is None
            or artifact.artifact_id
            != str(result_payload.get("deliberation_result_id") or "")
            or artifact.sha256 != canonical_sha256(result_payload)
        ):
            raise ValueError(
                "Deliberation Revision Result does not match the updated handoff"
            )
        if state.human_selection is not None:
            raise ValueError(
                "Upstream Deliberation Revision cannot bypass an existing Human Selection"
            )
        child_epoch = request.revision_epoch + 1
        child_request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.CONCLUSION,
            target_layer=LayerId.CONCLUSION,
            revision_epoch=child_epoch,
            source_review_id=result_message.message_id,
            source_finding_ids=request.source_finding_ids,
        )
        old_package = state.conclusion_package
        if old_package is None:
            raise ValueError("Conclusion upstream resume lost its reviewed package")
        child_request = RevisionRequestV1.create(
            revision_request_id=child_request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.INTERNAL,
            source_layer=LayerId.CONCLUSION,
            target_layer=LayerId.CONCLUSION,
            revision_epoch=child_epoch,
            root_revision_request_id=request.root_revision_request_id,
            parent_revision_request_id=request.revision_request_id,
            source_review_id=result_message.message_id,
            source_finding_ids=request.source_finding_ids,
            target_agent_ids=[
                POSITION_GENERATOR_ID,
                DECISION_EVALUATOR_ID,
                DECISION_INTEGRATOR_ID,
            ],
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="deliberation.deliberation_result",
                    artifact_id=deliberation_result.deliberation_result_id,
                    sha256=canonical_sha256(result_payload),
                ),
                RevisionArtifactRef(
                    artifact_type="conclusion.conclusion_package",
                    artifact_id=str(old_package["conclusion_package_id"]),
                    sha256=canonical_sha256(old_package),
                ),
            ],
            required_actions=[
                "Regenerate and re-evaluate Conclusion candidates from the revised Deliberation Result"
            ],
            acceptance_conditions=[
                f"{finding_id} is re-evaluated before Human Selection"
                for finding_id in request.source_finding_ids
            ],
            evidence_expansion_allowed=False,
            retrieval_allowed=False,
            expected_human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            created_at=result.completed_at,
        )
        child_message_id = str(
            uuid5(NAMESPACE_URL, f"{child_request_id}:request-message")
        )
        child_message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=result_message.message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id=POSITION_GENERATOR_ID,
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Rebuild Conclusion after a correlated Deliberation Revision",
                    payload=child_request.model_dump(mode="json"),
                    routing=PMPRouting(
                        revision_target=POSITION_GENERATOR_ID,
                        reply_required=True,
                    ),
                    metadata=PMPMetadata(
                        created_at=result.completed_at,
                        updated_at=result.completed_at,
                        status=MessageStatus.REVISION_REQUIRED,
                    ),
                ).model_dump(mode="json"),
                "message_id": child_message_id,
            }
        )
        self.revision_exchange.create_internal_request_once(child_message)
        for message in (result_message, handoff, child_message):
            if not any(item.message_id == message.message_id for item in state.message_history):
                state.message_history.append(message)
        state.deliberation_handoff = handoff.model_dump(mode="json")
        state.deliberation_result = result_payload
        state.decision_context = self._build_decision_context(
            deliberation_result
        ).model_dump(mode="json")
        state.revision_control = RevisionControlState(
            phase=RevisionControlPhase.AUTHORIZATION_REQUIRED,
            revision_epoch=child_epoch,
            active_request_id=child_request_id,
            active_request_message_id=child_message.message_id,
            root_revision_request_id=request.root_revision_request_id,
            parent_revision_request_id=request.revision_request_id,
            pending_request_ids=[child_request_id],
            consumed_request_ids=list(
                dict.fromkeys(
                    [*state.revision_control.consumed_request_ids, request.revision_request_id]
                )
            ),
            consumed_result_ids=list(
                dict.fromkeys(
                    [*state.revision_control.consumed_result_ids, result.revision_result_id]
                )
            ),
            audit_event_ids=list(state.revision_control.audit_event_ids),
        )
        state.status = WorkflowStatus.BLOCKED
        state.error = {
            "code": "UPSTREAM_RESULT_CONSUMED_AUTHORIZATION_REQUIRED",
            "message": "Deliberation result consumed; Conclusion regeneration requires authorization",
        }
        state.completed_at = None
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"result_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.RESULT_CONSUMED,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[deliberation_result.deliberation_result_id],
                reason="Correlated Deliberation result consumed before regeneration",
                created_at=result.completed_at,
            ),
        )
        self.repository.save(state)
        await self._emit(
            progress_callback,
            "Deliberation修正結果を受領。Conclusion再生成は別の明示承認待ちです",
        )
        if self.demo_safe_mode:
            return state
        return await self._execute_upstream_refresh_revision(
            state,
            actor_id=self.agent_id,
            actor_source="SYSTEM",
            reason="Safe Mode is disabled; regenerate after Deliberation Revision",
            progress_callback=progress_callback,
        )

    async def recover(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
        """Resume from the last valid Conclusion checkpoint without replaying it."""

        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError("Conclusion recovery requires a FAILED workflow")
        promoted_bindings = self._promote_saved_contract_repairs(state)
        result = self._validate_deliberation_handoff(
            PMPMessage.model_validate(state.deliberation_handoff)
        )
        context = DecisionContext.model_validate(state.decision_context)
        if result.workflow_id != state.workflow_id or context.workflow_id != state.workflow_id:
            raise ValueError("Conclusion recovery workflow identity mismatch")

        restored_task_ids = self._restore_saved_stage_responses(state, context)

        rerun_position = not self._saved_model_is_valid(
            state.position_generation,
            PositionGenerationResult,
        )
        rerun_evaluation = rerun_position or not self._saved_model_is_valid(
            state.decision_evaluation,
            DecisionEvaluationResult,
        )
        rerun_integration = rerun_evaluation or not self._saved_model_is_valid(
            state.decision_integration,
            DecisionIntegrationResult,
        )
        review_is_valid = self._saved_model_is_valid(
            state.review_result,
            ConclusionQualityReviewOutput,
        )
        next_agent = (
            POSITION_GENERATOR_ID
            if rerun_position
            else DECISION_EVALUATOR_ID
            if rerun_evaluation
            else DECISION_INTEGRATOR_ID
            if rerun_integration
            else QUALITY_REVIEWER_ID
            if not review_is_valid
            else None
        )
        task_id_overrides: dict[str, str] = {}
        model_overrides: dict[str, str] = {}
        if next_agent is None:
            saved_review_task_id = restored_task_ids.get(QUALITY_REVIEWER_ID)
            if saved_review_task_id:
                task_id_overrides[QUALITY_REVIEWER_ID] = saved_review_task_id
        else:
            try:
                request, _error_response = self._latest_failed_provider_exchange(state)
            except ValueError:
                request = None
            if request is not None:
                if request.receiver_agent_id != next_agent:
                    if request.receiver_agent_id in restored_task_ids:
                        # A persisted billed response was repaired and promoted
                        # without a second Provider call. Continue at its first
                        # unfinished dependent instead of requiring an unrelated
                        # retry authorization.
                        request = None
                    else:
                        raise ValueError(
                            "Saved Conclusion checkpoints do not match the failed Provider stage"
                        )
            if request is not None:
                original_task_id = request.payload.get("task_id")
                if not isinstance(original_task_id, str) or not original_task_id:
                    raise ValueError("Failed Conclusion request has no logical task_id")
                agent = self.registry.get(request.receiver_agent_id)
                provider_id = getattr(agent.provider, "provider_id", None)
                if not isinstance(provider_id, str):
                    raise ValueError("Conclusion Provider has no stable logical provider ID")
                if original_task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX):
                    raise ValueError(
                        "The one-time Conclusion provider contract repair is exhausted"
                    )
                if original_task_id.endswith(
                    CANDIDATE_COVERAGE_CONTRACT_REPAIR_SUFFIX
                ):
                    raise ValueError(
                        "The one-time Conclusion candidate coverage contract repair is exhausted"
                    )
                if original_task_id.endswith(OPERATOR_RETRY_SUFFIX):
                    base_task_id = original_task_id[: -len(OPERATOR_RETRY_SUFFIX)]
                    repair_authorization = (
                        self.provider_contract_repair_store.for_original_task(
                            workflow_id=workflow_id,
                            provider_id=provider_id,
                            original_task_id=base_task_id,
                        )
                    )
                    if (
                        repair_authorization is None
                        or repair_authorization.status != "PENDING"
                        or repair_authorization.retry_task_id != original_task_id
                        or repair_authorization.source_error_message_id
                        != _error_response.message_id
                    ):
                        raise ValueError(
                            "Conclusion recovery found an exhausted contract-invalid retry; "
                            "explicit provider contract repair authorization is required"
                        )
                    if self._has_unanswered_task_request(
                        state,
                        next_agent,
                        repair_authorization.repair_task_id,
                    ):
                        raise ValueError(
                            "Conclusion recovery found an unanswered provider contract "
                            "repair request; automatic redispatch is blocked"
                        )
                    self._clear_from_failed_stage(state, next_agent)
                    task_id_overrides[next_agent] = (
                        repair_authorization.repair_task_id
                    )
                    model_overrides[next_agent] = (
                        repair_authorization.repair_model_id
                    )
                elif self._is_candidate_coverage_contract_failure(
                    state,
                    request,
                    _error_response,
                ):
                    repair_task_id = (
                        original_task_id
                        + CANDIDATE_COVERAGE_CONTRACT_REPAIR_SUFFIX
                    )
                    if self._has_unanswered_task_request(
                        state,
                        next_agent,
                        repair_task_id,
                    ):
                        raise ValueError(
                            "Conclusion recovery found an unanswered candidate coverage "
                            "contract repair request; automatic redispatch is blocked"
                        )
                    self._clear_from_failed_stage(state, next_agent)
                    self._record_candidate_coverage_audit(
                        state,
                        task_id=original_task_id,
                        payload=_error_response.payload.get("invalid_payload"),
                        recovery_task_id=repair_task_id,
                    )
                    task_id_overrides[next_agent] = repair_task_id
                else:
                    authorization = self.provider_retry_store.for_original_task(
                        workflow_id=workflow_id,
                        provider_id=provider_id,
                        original_task_id=original_task_id,
                    )
                    if authorization is None or authorization.status != "PENDING":
                        raise ValueError(
                            "Conclusion recovery found a Provider call without a reusable response; "
                            "explicit provider retry authorization is required"
                        )
                    if self._has_unanswered_task_request(
                        state,
                        next_agent,
                        authorization.retry_task_id,
                    ):
                        raise ValueError(
                            "Conclusion recovery found an unanswered provider retry request; "
                            "automatic redispatch is blocked"
                        )
                    self._clear_from_failed_stage(state, next_agent)
                    task_id_overrides[next_agent] = authorization.retry_task_id
            elif self._has_unanswered_stage_request(state, next_agent):
                raise ValueError(
                    "Conclusion recovery found an unanswered Provider request; "
                    "an explicit audited retry path is required"
                )

        state.error = None
        state.current_agent_ids = []
        state.failed_agents = [
            agent_id
            for agent_id in state.failed_agents
            if agent_id != next_agent and agent_id not in restored_task_ids
        ]
        manager_snapshot = self.rd_loader.load(self.agent_id)
        if manager_snapshot.trace() not in state.role_definition_usage:
            state.role_definition_usage.append(manager_snapshot.trace())
        self.repository.save(state)
        await self._emit(
            progress_callback,
            "Conclusion checkpoint recovery: reusing completed stages; next stage "
            + (next_agent or "saved quality decision"),
        )
        if promoted_bindings:
            await self._emit(
                progress_callback,
                "Restored verified Provider model compatibility: "
                + ", ".join(promoted_bindings),
            )
        return await self._run_generation_and_review(
            state,
            rerun_position=rerun_position,
            rerun_evaluation=rerun_evaluation,
            rerun_integration=rerun_integration,
            task_id_overrides=task_id_overrides,
            model_overrides=model_overrides,
            progress_callback=progress_callback,
        )

    async def revise(
        self,
        workflow_id: str,
        *,
        actor_id: str = "cli.operator",
        actor_source: str = "CLI",
        reason: str = "Operator authorized Conclusion revision",
        progress_callback: ProgressCallback | None = None,
    ) -> ConclusionWorkflowState:
        """Run one operator-authorized internal Conclusion revision in Safe Mode."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Explicit Conclusion revision is available only while Demo Safe Mode "
                "is enabled"
            )
        state = self.repository.load(workflow_id)
        self._promote_saved_contract_repairs(state)
        self._activate_pending_playwright_revision(state)
        if state.status != WorkflowStatus.BLOCKED.value:
            raise ValueError(
                "Conclusion must be BLOCKED at a saved Quality Review before an "
                "explicit revision"
            )
        if (
            state.revision_control.phase
            == RevisionControlPhase.AUTHORIZATION_REQUIRED.value
        ):
            if state.revision_count >= self.max_revisions:
                return await self._block(
                    state,
                    f"Quality Reviewer revision上限{self.max_revisions}回に達したため停止しました",
                    progress_callback,
                )
            return await self._execute_upstream_refresh_revision(
                state,
                actor_id=actor_id,
                actor_source=actor_source,
                reason=reason,
                progress_callback=progress_callback,
            )
        if state.review_result is None:
            raise ValueError("Conclusion has no saved Quality Review decision")
        review = ConclusionQualityReviewOutput.model_validate(state.review_result)
        if review.status != QualityGateDecision.REVISION_REQUIRED.value:
            raise ValueError(
                "Explicit Conclusion revision requires a revision_required review"
            )
        if review.revision_scope == RevisionScope.DELIBERATION_RETURN.value:
            raise ValueError(
                "Deliberation return must use the Conclusion upstream resume path"
            )
        if not review.revision_targets:
            raise ValueError("Explicit Conclusion revision has no internal targets")

        effective_targets, routing_finding = self._resolve_explicit_revision_targets(
            state,
            review,
        )
        manager_repair = self._manager_package_repair_plan(
            state,
            review,
            effective_targets=effective_targets,
        )
        if manager_repair is not None:
            current_repairs = [
                record
                for record in state.manager_repair_history
                if record.upstream_revision_count == state.upstream_revision_count
                and record.revision_count == state.revision_count
            ]
            if len(current_repairs) >= self.max_manager_repairs_per_revision:
                return await self._block(
                    state,
                    "The bounded Conclusion Manager package repair is exhausted",
                    progress_callback,
                )
            repair_iteration = len(state.manager_repair_history) + 1
            review_task_id = (
                f"conclusion_quality_review_upstream_{state.upstream_revision_count}"
                f"_revision_{state.revision_count}_manager_repair_{repair_iteration}"
            )
            state.manager_repair_history.append(
                ConclusionManagerRepairRecord(
                    iteration=repair_iteration,
                    upstream_revision_count=state.upstream_revision_count,
                    revision_count=state.revision_count,
                    source_review_id=review.review_id,
                    source_finding_ids=[item.finding_id for item in review.findings],
                    repair_kind="alternative_materialization",
                    added_alternative_candidate_ids=manager_repair[
                        "added_alternative_candidate_ids"
                    ],
                    reviewer_task_id=review_task_id,
                )
            )
            state.conclusion_package = manager_repair["package"].model_dump(mode="json")
            state.deterministic_validation = manager_repair["validation"].model_dump(
                mode="json"
            )
            state.review_result = None
            state.completed_agents = [
                agent_id
                for agent_id in state.completed_agents
                if agent_id != QUALITY_REVIEWER_ID
            ]
            state.failed_agents = [
                agent_id
                for agent_id in state.failed_agents
                if agent_id != QUALITY_REVIEWER_ID
            ]
            state.current_agent_ids = []
            state.status = WorkflowStatus.REVISING
            state.error = None
            state.completed_at = None
            self.repository.save(state)
            await self._emit(
                progress_callback,
                "Operator-authorized bounded Manager package repair: "
                + ", ".join(manager_repair["added_alternative_candidate_ids"]),
            )
            return await self._run_generation_and_review(
                state,
                rerun_position=False,
                rerun_evaluation=False,
                rerun_integration=False,
                task_id_overrides={QUALITY_REVIEWER_ID: review_task_id},
                progress_callback=progress_callback,
            )
        self._plan_internal_revision(
            state,
            review=review,
            target_agent_ids=effective_targets,
            routing_finding=routing_finding,
        )
        self.repository.save(state)
        return await self._execute_upstream_refresh_revision(
            state,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            progress_callback=progress_callback,
        )

    def _activate_pending_playwright_revision(
        self,
        state: ConclusionWorkflowState,
    ) -> PMPMessage | None:
        if (
            state.revision_control.phase
            in {
                RevisionControlPhase.AUTHORIZATION_REQUIRED.value,
                RevisionControlPhase.EXECUTING.value,
            }
            and state.revision_control.active_request_id
        ):
            return None
        candidates = self.revision_exchange.list_requests(
            target_layer=LayerId.CONCLUSION,
            workflow_id=state.workflow_id,
        )
        message = next(
            (
                item
                for item in reversed(candidates)
                if RevisionRequestV1.model_validate(item.payload).source_layer
                == LayerId.PLAYWRIGHT.value
                and RevisionRequestV1.model_validate(item.payload).revision_request_id
                not in state.revision_control.consumed_request_ids
            ),
            None,
        )
        if message is None:
            return None
        request = RevisionRequestV1.model_validate(message.payload)
        self.revision_exchange.validator.validate_current_base_artifacts(
            request,
            {
                (
                    "conclusion.final_conclusion",
                    str((state.final_conclusion or {}).get("final_conclusion_id") or ""),
                ): canonical_sha256(state.final_conclusion),
                (
                    "conclusion.conclusion_package",
                    str((state.conclusion_package or {}).get("conclusion_package_id") or ""),
                ): canonical_sha256(state.conclusion_package),
                (
                    "conclusion.human_selection",
                    str((state.human_selection or {}).get("selection_id") or ""),
                ): canonical_sha256(state.human_selection),
            },
        )
        if not any(item.message_id == message.message_id for item in state.message_history):
            state.message_history.append(message)
        state.revision_control = RevisionControlState(
            phase=RevisionControlPhase.AUTHORIZATION_REQUIRED,
            revision_epoch=request.revision_epoch,
            active_request_id=request.revision_request_id,
            active_request_message_id=message.message_id,
            root_revision_request_id=request.root_revision_request_id,
            parent_revision_request_id=request.parent_revision_request_id,
            pending_request_ids=[request.revision_request_id],
            consumed_request_ids=list(state.revision_control.consumed_request_ids),
            consumed_result_ids=list(state.revision_control.consumed_result_ids),
            audit_event_ids=list(state.revision_control.audit_event_ids),
        )
        state.status = WorkflowStatus.BLOCKED
        state.error = {
            "code": "PLAYWRIGHT_REVISION_AUTHORIZATION_REQUIRED",
            "message": "Playwright Revision Request is valid and requires Conclusion authorization",
        }
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"request_consumed_boundary_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.REQUEST_CONSUMED,
                actor_id=self.agent_id,
                message_id=message.message_id,
                reason="Validated Playwright request and entered authorization boundary",
            ),
        )
        self.repository.save(state)
        return message

    def _plan_internal_revision(
        self,
        state: ConclusionWorkflowState,
        *,
        review: ConclusionQualityReviewOutput,
        target_agent_ids: list[str],
        routing_finding: dict[str, Any] | None = None,
    ) -> PMPMessage:
        """Persist one canonical, provider-free Conclusion revision plan."""

        if state.conclusion_package is None:
            raise ValueError("Conclusion revision requires a saved package")
        findings = [item.model_dump(mode="json") for item in review.findings]
        if routing_finding is not None:
            findings.append(routing_finding)
        finding_ids = list(
            dict.fromkeys(str(item["finding_id"]) for item in findings)
        )
        if not finding_ids:
            raise ValueError("Conclusion revision requires at least one finding")
        epoch = max(state.revision_control.revision_epoch, state.revision_count) + 1
        parent_request_id = (
            state.revision_control.active_request_id
            if state.revision_control.phase
            in {
                RevisionControlPhase.COMPLETED.value,
                RevisionControlPhase.BLOCKED.value,
            }
            else None
        )
        root_request_id = state.revision_control.root_revision_request_id
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.CONCLUSION,
            target_layer=LayerId.CONCLUSION,
            revision_epoch=epoch,
            source_review_id=review.review_id,
            source_finding_ids=finding_ids,
        )
        if parent_request_id is None:
            root_request_id = request_id
        request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.INTERNAL,
            source_layer=LayerId.CONCLUSION,
            target_layer=LayerId.CONCLUSION,
            revision_epoch=epoch,
            root_revision_request_id=root_request_id or request_id,
            parent_revision_request_id=parent_request_id,
            source_review_id=review.review_id,
            source_finding_ids=finding_ids,
            target_agent_ids=list(dict.fromkeys(target_agent_ids)),
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="deliberation.deliberation_result",
                    artifact_id=str(
                        state.deliberation_result["deliberation_result_id"]
                    ),
                    sha256=canonical_sha256(state.deliberation_result),
                ),
                RevisionArtifactRef(
                    artifact_type="conclusion.conclusion_package",
                    artifact_id=str(
                        state.conclusion_package["conclusion_package_id"]
                    ),
                    sha256=canonical_sha256(state.conclusion_package),
                ),
            ],
            required_actions=list(
                dict.fromkeys(
                    str(item.get("required_action") or item.get("issue") or review.reason)
                    for item in findings
                )
            ),
            acceptance_conditions=[
                f"Resolve or explicitly retain finding {finding_id}"
                for finding_id in finding_ids
            ],
            evidence_expansion_allowed=False,
            retrieval_allowed=False,
            expected_human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
        )
        first_agent = self._first_revision_agent(request.target_agent_ids)
        source_message = next(
            (
                item
                for item in reversed(state.message_history)
                if item.sender_agent_id == QUALITY_REVIEWER_ID
            ),
            state.message_history[-1],
        )
        message_id = str(uuid5(NAMESPACE_URL, f"{request_id}:request-message"))
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=source_message.message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id=first_agent,
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Run the minimum Conclusion dependency closure required by Quality Review",
                    payload=request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="conclusion.revision_planned",
                        previous_stage=QUALITY_REVIEWER_ID,
                        next_stage=first_agent,
                    ),
                    routing=PMPRouting(
                        revision_target=first_agent,
                        reply_required=True,
                    ),
                    metadata=PMPMetadata(status=MessageStatus.REVISION_REQUIRED),
                ).model_dump(mode="json"),
                "message_id": message_id,
            }
        )
        self.revision_exchange.create_internal_request_once(message)
        if not any(item.message_id == message.message_id for item in state.message_history):
            state.message_history.append(message)
        state.revision_control = RevisionControlState(
            phase=RevisionControlPhase.AUTHORIZATION_REQUIRED,
            revision_epoch=epoch,
            active_request_id=request_id,
            active_request_message_id=message.message_id,
            root_revision_request_id=request.root_revision_request_id,
            parent_revision_request_id=request.parent_revision_request_id,
            pending_request_ids=[request_id],
            consumed_request_ids=list(state.revision_control.consumed_request_ids),
            consumed_result_ids=list(state.revision_control.consumed_result_ids),
            audit_event_ids=list(state.revision_control.audit_event_ids),
        )
        state.status = WorkflowStatus.BLOCKED
        state.error = {
            "code": "REVISION_AUTHORIZATION_REQUIRED",
            "message": (
                "Demo Safe Mode saved the Conclusion revision plan and requires "
                "explicit authorization"
            ),
        }
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"request_written_{epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=message.message_id,
                artifact_ids=[item.artifact_id for item in request.base_artifacts],
                reason=review.reason,
            ),
        )
        return message

    async def _execute_upstream_refresh_revision(
        self,
        state: ConclusionWorkflowState,
        *,
        actor_id: str,
        actor_source: str,
        reason: str,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        """Authorize and execute the active canonical internal Conclusion plan."""

        if (
            state.revision_control.phase
            == RevisionControlPhase.COMPLETED.value
            and state.revision_control.active_result_id
        ):
            return state
        if (
            state.revision_control.phase
            not in {
                RevisionControlPhase.AUTHORIZATION_REQUIRED.value,
                RevisionControlPhase.EXECUTING.value,
            }
        ):
            raise ValueError("Conclusion has no executable Revision authorization boundary")
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        incoming_playwright = (
            request.route == RevisionRoute.UPSTREAM.value
            and request.source_layer == LayerId.PLAYWRIGHT.value
            and request.target_layer == LayerId.CONCLUSION.value
        )
        if request.route != RevisionRoute.INTERNAL.value and not incoming_playwright:
            raise ValueError("Conclusion execution accepts only its owned Revision routes")
        self.revision_exchange.validator.validate_current_base_artifacts(
            request,
            {
                (
                    "deliberation.deliberation_result",
                    str(state.deliberation_result.get("deliberation_result_id") or ""),
                ): canonical_sha256(state.deliberation_result),
                (
                    "conclusion.conclusion_package",
                    str((state.conclusion_package or {}).get("conclusion_package_id") or ""),
                ): canonical_sha256(state.conclusion_package),
                (
                    "conclusion.final_conclusion",
                    str((state.final_conclusion or {}).get("final_conclusion_id") or ""),
                ): canonical_sha256(state.final_conclusion),
                (
                    "conclusion.human_selection",
                    str((state.human_selection or {}).get("selection_id") or ""),
                ): canonical_sha256(state.human_selection),
            },
        )
        provider_tasks = (
            {}
            if incoming_playwright
            and request.expected_human_selection_impact
            == HumanSelectionImpact.UNCHANGED.value
            else self._revision_execution_identities(state, request)
        )
        authorization = RevisionExecutionAuthorization(
            authorization_id=(
                "revision_authorization_"
                + uuid5(NAMESPACE_URL, request.revision_request_id).hex
            ),
            workflow_id=state.workflow_id,
            revision_request_id=request.revision_request_id,
            executing_layer=LayerId.CONCLUSION,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            max_provider_calls=len(provider_tasks),
            max_retrieval_calls=0,
        )
        try:
            existing_authorization = self.revision_exchange.load_authorization(
                executing_layer=LayerId.CONCLUSION,
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
            )
            if (
                existing_authorization.actor_id != actor_id
                or existing_authorization.actor_source != actor_source
                or existing_authorization.reason != reason
            ):
                raise ValueError(
                    "Conclusion Revision is already authorized by a different actor"
                )
            authorization = existing_authorization
        except FileNotFoundError:
            self.revision_exchange.create_authorization_once(authorization)
            self._record_revision_audit(
                state,
                RevisionAuditEvent(
                    audit_event_id=f"authorization_created_{request.revision_epoch}",
                    workflow_id=state.workflow_id,
                    revision_request_id=request.revision_request_id,
                    layer=LayerId.CONCLUSION,
                    event_type=RevisionAuditEventType.AUTHORIZATION_CREATED,
                    actor_id=actor_id,
                    reason=reason,
                ),
            )
        try:
            if request.route == RevisionRoute.INTERNAL.value:
                budget = self.revision_exchange.budget_store.consume(
                    policy=RevisionBudgetPolicy(
                        internal_limit=self.max_revisions,
                        upstream_limit=self.max_revisions,
                    ),
                    workflow_id=state.workflow_id,
                    layer=LayerId.CONCLUSION,
                    route=RevisionRoute.INTERNAL,
                    revision_request_id=request.revision_request_id,
                )
            else:
                budget = self.revision_exchange.budget_store.for_request(
                    workflow_id=state.workflow_id,
                    layer=LayerId.PLAYWRIGHT,
                    route=RevisionRoute.UPSTREAM,
                    revision_request_id=request.revision_request_id,
                )
                if budget is None:
                    raise RevisionBudgetExhausted(
                        "Playwright upstream Revision Request has no consumed budget slot"
                    )
        except RevisionBudgetExhausted as exc:
            state.revision_count = max(
                state.revision_count,
                min(self.max_revisions, request.revision_epoch),
            )
            state.revision_control.phase = RevisionControlPhase.BLOCKED
            state.status = WorkflowStatus.BLOCKED
            state.error = {"code": "REVISION_BUDGET_EXHAUSTED", "message": str(exc)}
            self._record_revision_audit(
                state,
                RevisionAuditEvent(
                    audit_event_id=f"budget_blocked_{request.revision_epoch}",
                    workflow_id=state.workflow_id,
                    revision_request_id=request.revision_request_id,
                    layer=LayerId.CONCLUSION,
                    event_type=RevisionAuditEventType.BLOCKED,
                    actor_id=actor_id,
                    reason=str(exc),
                ),
            )
            self.repository.save(state)
            return state
        consumed = self.revision_exchange.consume_authorization(
            authorization,
            provider_reservation_ids=list(provider_tasks.values()),
            retrieval_reservation_ids=[],
        )
        for event in (
            RevisionAuditEvent(
                audit_event_id=f"authorization_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.AUTHORIZATION_CONSUMED,
                actor_id=actor_id,
                reservation_ids=list(provider_tasks.values()),
                reason=reason,
                created_at=consumed.consumed_at,
            ),
            RevisionAuditEvent(
                audit_event_id=f"budget_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.BUDGET_CONSUMED,
                actor_id=self.agent_id,
                reason=f"Conclusion {request.route} revision slot {budget.iteration}",
                created_at=budget.consumed_at,
            ),
            RevisionAuditEvent(
                audit_event_id=f"request_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.REQUEST_CONSUMED,
                actor_id=self.agent_id,
                message_id=request_message.message_id,
                reason="Conclusion Manager began the saved revision plan",
                created_at=budget.consumed_at,
            ),
        ):
            self._record_revision_audit(state, event)
        state.revision_control.phase = RevisionControlPhase.EXECUTING
        if request.route == RevisionRoute.INTERNAL.value:
            state.revision_count = max(state.revision_count, budget.iteration)
        if request.route == RevisionRoute.INTERNAL.value and not any(
            record.iteration == budget.iteration
            for record in state.revision_history
        ):
            state.revision_history.append(
                ConclusionRevisionRecord(
                    iteration=budget.iteration,
                    target_agent_ids=request.target_agent_ids,
                    findings=[
                        {"finding_id": finding_id, "source": request.source_review_id}
                        for finding_id in request.source_finding_ids
                    ],
                    rerun_stages=self._revision_stages(request.target_agent_ids),
                )
            )
        if (
            incoming_playwright
            and request.expected_human_selection_impact
            == HumanSelectionImpact.UNCHANGED.value
        ):
            final = FinalConclusion.model_validate(state.final_conclusion)
            handoff = self._send_to_playwright(state, final)
            state.playwright_sent = True
            self._finalize_active_revision(
                state,
                review_response=handoff,
                completed=True,
                reason="Reissued structurally corrected Conclusion handoff without semantic change",
            )
            state.status = WorkflowStatus.COMPLETED
            state.error = None
            state.completed_at = utc_now()
            self.repository.save(state)
            return state
        first_agent = self._first_revision_agent(request.target_agent_ids)
        self._clear_from_failed_stage(state, first_agent)
        state.revision_control.phase = RevisionControlPhase.EXECUTING
        state.status = WorkflowStatus.REVISING
        state.error = None
        state.completed_at = None
        self.repository.save(state)
        await self._emit(
            progress_callback,
            "Authorized Conclusion revision → "
            + ", ".join(request.target_agent_ids)
            + f"（{budget.iteration}/{self.max_revisions}）",
        )
        rerun_position = first_agent == POSITION_GENERATOR_ID
        rerun_evaluation = first_agent in {
            POSITION_GENERATOR_ID,
            DECISION_EVALUATOR_ID,
        }
        rerun_integration = first_agent in {
            POSITION_GENERATOR_ID,
            DECISION_EVALUATOR_ID,
            DECISION_INTEGRATOR_ID,
        }
        return await self._run_generation_and_review(
            state,
            rerun_position=rerun_position,
            rerun_evaluation=rerun_evaluation,
            rerun_integration=rerun_integration,
            operation_variant=(
                f"revision_{request.revision_epoch}_"
                f"{request.revision_request_id.rsplit('_', 1)[-1][:12]}"
            ),
            task_id_overrides=provider_tasks,
            progress_callback=progress_callback,
        )

    @staticmethod
    def _first_revision_agent(target_agent_ids: list[str]) -> str:
        order = [
            POSITION_GENERATOR_ID,
            DECISION_EVALUATOR_ID,
            DECISION_INTEGRATOR_ID,
            "conclusion.manager",
            QUALITY_REVIEWER_ID,
        ]
        indices = [order.index(item) for item in target_agent_ids if item in order]
        if not indices:
            raise ValueError("Conclusion Revision has no executable target")
        selected = order[min(indices)]
        return DECISION_INTEGRATOR_ID if selected == "conclusion.manager" else selected

    def _revision_execution_identities(
        self,
        state: ConclusionWorkflowState,
        request: RevisionRequestV1,
    ) -> dict[str, str]:
        order = [
            POSITION_GENERATOR_ID,
            DECISION_EVALUATOR_ID,
            DECISION_INTEGRATOR_ID,
            QUALITY_REVIEWER_ID,
        ]
        first_agent = self._first_revision_agent(request.target_agent_ids)
        stage_names = {
            POSITION_GENERATOR_ID: "position_generation",
            DECISION_EVALUATOR_ID: "decision_evaluation",
            DECISION_INTEGRATOR_ID: "decision_integration",
            QUALITY_REVIEWER_ID: "quality_review",
        }
        return {
            agent_id: (
                f"conclusion_{stage_names[agent_id]}_upstream_"
                f"{state.upstream_revision_count}_revision_{request.revision_epoch}"
            )
            for agent_id in order[order.index(first_agent) :]
        }

    def _finalize_active_revision(
        self,
        state: ConclusionWorkflowState,
        *,
        review_response: PMPMessage,
        completed: bool,
        reason: str,
    ) -> None:
        if state.revision_control.phase != RevisionControlPhase.EXECUTING.value:
            return
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        result_artifacts: list[RevisionArtifactRef] = []
        incoming_playwright = (
            request.route == RevisionRoute.UPSTREAM.value
            and request.source_layer == LayerId.PLAYWRIGHT.value
        )
        if incoming_playwright and state.final_conclusion is not None:
            final_id = str(
                state.final_conclusion.get("final_conclusion_id") or ""
            )
            if final_id:
                result_artifacts.append(
                    RevisionArtifactRef(
                        artifact_type="conclusion.final_conclusion",
                        artifact_id=final_id,
                        sha256=canonical_sha256(state.final_conclusion),
                    )
                )
        elif state.conclusion_package is not None:
            package_id = str(
                state.conclusion_package.get("conclusion_package_id") or ""
            )
            if package_id:
                result_artifacts.append(
                    RevisionArtifactRef(
                        artifact_type="conclusion.conclusion_package",
                        artifact_id=package_id,
                        sha256=canonical_sha256(state.conclusion_package),
                    )
                )
        provider_tasks = (
            {}
            if incoming_playwright
            and request.expected_human_selection_impact
            == HumanSelectionImpact.UNCHANGED.value
            else self._revision_execution_identities(state, request)
        )
        result_id = "revision_result_" + uuid5(
            NAMESPACE_URL,
            f"{request.revision_request_id}:result",
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
            human_selection_impact=(
                request.expected_human_selection_impact
                if incoming_playwright
                else HumanSelectionImpact.NOT_APPLICABLE
            ),
            provider_reservation_ids=list(provider_tasks.values()),
            retrieval_reservation_ids=[],
            provider_call_count=len(provider_tasks),
            retrieval_call_count=0,
            completed_at=review_response.metadata.updated_at,
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
                    objective="Return the audited Conclusion internal Revision result",
                    payload=result.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="conclusion.revision_result",
                        previous_stage=QUALITY_REVIEWER_ID,
                        next_stage="conclusion.manager",
                    ),
                    routing=PMPRouting(revision_target=None, reply_required=False),
                    metadata=PMPMetadata(
                        created_at=review_response.metadata.updated_at,
                        updated_at=review_response.metadata.updated_at,
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
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.RESULT_WRITTEN,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[item.artifact_id for item in result_artifacts],
                reservation_ids=list(provider_tasks.values()),
                reason=reason,
                created_at=review_response.metadata.updated_at,
            ),
        )
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": (
                    RevisionControlPhase.COMPLETED.value
                    if completed
                    else RevisionControlPhase.BLOCKED.value
                ),
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

    def _resolve_explicit_revision_targets(
        self,
        state: ConclusionWorkflowState,
        review: ConclusionQualityReviewOutput,
    ) -> tuple[list[str], dict[str, Any] | None]:
        """Narrow a trace-only review to the earliest invalid saved artifact.

        Older checkpoints may have passed a less complete deterministic
        validator. Revalidation is read-only with respect to the artifacts: it
        identifies their true producer and avoids replaying valid predecessors.
        """

        original_targets = [str(item) for item in review.revision_targets]
        if not review.findings or any(
            finding.category not in {"traceability", "unsupported_claim"}
            for finding in review.findings
        ):
            return original_targets, None
        try:
            context = DecisionContext.model_validate(state.decision_context)
            generation = PositionGenerationResult.model_validate(
                state.position_generation
            )
            evaluation = DecisionEvaluationResult.model_validate(
                state.decision_evaluation
            )
            integration = DecisionIntegrationResult.model_validate(
                state.decision_integration
            )
            package = ConclusionPackage.model_validate(state.conclusion_package)
        except Exception:
            return original_targets, None

        candidate_ids = {
            item.position_candidate_id for item in generation.position_candidates
        }
        artifacts = (
            (POSITION_GENERATOR_ID, generation),
            (DECISION_EVALUATOR_ID, evaluation),
            (DECISION_INTEGRATOR_ID, integration),
            (self.agent_id, package),
        )
        for target, artifact in artifacts:
            violations = self.deterministic_validator.unknown_reference_ids(
                decision_context=context,
                value=artifact,
                candidate_ids=candidate_ids,
            )
            if not violations:
                continue
            effective_targets = [target]
            if effective_targets == original_targets:
                return effective_targets, None
            affected_ids = sorted({item["id"] for item in violations})
            return effective_targets, {
                "finding_id": (
                    f"deterministic_reference_routing_revision_"
                    f"{state.revision_count + 1}"
                ),
                "severity": "CRITICAL",
                "category": "traceability",
                "issue": (
                    "Current deterministic reference validation located the "
                    f"earliest invalid artifact at {target}"
                ),
                "required_action": (
                    "Rerun the earliest invalid producer and its dependency "
                    "closure without replaying valid predecessors"
                ),
                "affected_agent_ids": [target],
                "affected_candidate_ids": [],
                "affected_reference_ids": affected_ids,
                "original_revision_targets": original_targets,
            }
        return original_targets, None

    def _manager_package_repair_plan(
        self,
        state: ConclusionWorkflowState,
        review: ConclusionQualityReviewOutput,
        *,
        effective_targets: list[str],
    ) -> dict[str, Any] | None:
        """Plan the one bounded repair that never reruns a specialist Agent.

        Eligibility is intentionally structural. Reviewer prose is not parsed and
        no field is invented: the repaired alternatives are derived entirely from
        saved Integration and Evaluation artifacts. Any unrelated deterministic
        finding keeps the ordinary revision ceiling in force.
        """

        if (
            effective_targets != [self.agent_id]
            or review.upstream_revision_requests
            or not review.findings
            or any(
                set(finding.affected_agent_ids) - {self.agent_id}
                for finding in review.findings
            )
        ):
            return None
        try:
            context = DecisionContext.model_validate(state.decision_context)
            generation = PositionGenerationResult.model_validate(
                state.position_generation
            )
            evaluation = DecisionEvaluationResult.model_validate(
                state.decision_evaluation
            )
            integration = DecisionIntegrationResult.model_validate(
                state.decision_integration
            )
            package = ConclusionPackage.model_validate(state.conclusion_package)
        except Exception:
            return None

        current_validation = self.deterministic_validator.validate(
            decision_context=context,
            position_generation=generation,
            decision_evaluation=evaluation,
            decision_integration=integration,
            conclusion_package=package,
            human_selection_present=state.human_selection is not None,
        )
        repairable_categories = {"alternative_coverage", "alternative_detail"}
        if (
            current_validation.passed
            or not current_validation.findings
            or any(
                finding.category not in repairable_categories
                for finding in current_validation.findings
            )
        ):
            return None

        repaired_package = self._build_package(
            state,
            context,
            generation,
            evaluation,
            integration,
        )
        repaired_validation = self.deterministic_validator.validate(
            decision_context=context,
            position_generation=generation,
            decision_evaluation=evaluation,
            decision_integration=integration,
            conclusion_package=repaired_package,
            human_selection_present=state.human_selection is not None,
        )
        if not repaired_validation.passed:
            return None

        prior_ids = {
            str(item.get("candidate_id"))
            for item in package.alternatives
            if isinstance(item, dict) and item.get("candidate_id")
        }
        repaired_ids = {
            str(item.get("candidate_id"))
            for item in repaired_package.alternatives
            if isinstance(item, dict) and item.get("candidate_id")
        }
        added_ids = sorted(repaired_ids - prior_ids)
        if not added_ids:
            return None
        return {
            "package": repaired_package,
            "validation": repaired_validation,
            "added_alternative_candidate_ids": added_ids,
        }

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
        integration_identity = json.dumps(
            {
                "candidate_ids": sorted(candidate_ids),
                "user_instruction": user_instruction,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        operation_variant = "human_integration_" + hashlib.sha256(
            integration_identity.encode("utf-8")
        ).hexdigest()[:12]
        return await self._run_generation_and_review(
            state,
            rerun_position=False,
            rerun_evaluation=False,
            rerun_integration=True,
            requested_integration_candidate_ids=candidate_ids,
            user_instruction=user_instruction,
            operation_variant=operation_variant,
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
            playwright_handoff = self._send_to_playwright(state, final)
        except Exception as exc:
            state.status = WorkflowStatus.FAILED
            state.error = {"stage": "playwright_handoff", "message": str(exc)}
            self.repository.save(state)
            return state
        state.playwright_sent = True
        if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
            active_request = RevisionRequestV1.model_validate(
                self._active_revision_request_message(state).payload
            )
            if (
                active_request.route == RevisionRoute.UPSTREAM.value
                and active_request.source_layer == LayerId.PLAYWRIGHT.value
            ):
                self._finalize_active_revision(
                    state,
                    review_response=playwright_handoff,
                    completed=True,
                    reason="Human re-selection completed the Playwright-requested Conclusion revision",
                )
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
        operation_variant: str | None = None,
        task_id_overrides: dict[str, str] | None = None,
        model_overrides: dict[str, str] | None = None,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        task_id_overrides = task_id_overrides or {}
        model_overrides = model_overrides or {}
        while True:
            try:
                context, _removed_context_references = (
                    self.deterministic_validator.canonical_decision_context_view(
                        DecisionContext.model_validate(state.decision_context)
                    )
                )
                if rerun_position:
                    state.status = WorkflowStatus.GENERATING_POSITIONS
                    generation = await self._generate_positions(
                        state,
                        context,
                        task_id=task_id_overrides.get(POSITION_GENERATOR_ID),
                        operation_variant=operation_variant,
                        model_override=model_overrides.get(POSITION_GENERATOR_ID),
                    )
                    state.position_generation = generation.model_dump(mode="json")
                    state.position_candidates = [
                        item.model_dump(mode="json") for item in generation.position_candidates
                    ]
                    self.repository.save(state)
                    await self._emit(progress_callback, f"Position Generator完了: {len(state.position_candidates)}候補")
                else:
                    generation = PositionGenerationResult.model_validate(state.position_generation)

                if (
                    not rerun_position
                    and not rerun_evaluation
                    and self._saved_model_is_valid(
                        state.evaluation_framework,
                        EvaluationFramework,
                    )
                ):
                    framework = EvaluationFramework.model_validate(
                        state.evaluation_framework
                    )
                else:
                    framework = self._build_evaluation_framework(context)
                    state.evaluation_framework = framework.model_dump(mode="json")
                if rerun_position or rerun_evaluation:
                    state.status = WorkflowStatus.EVALUATING_POSITIONS
                    evaluation = await self._evaluate_positions(
                        state,
                        context,
                        generation,
                        framework,
                        task_id=task_id_overrides.get(DECISION_EVALUATOR_ID),
                        operation_variant=operation_variant,
                        model_override=model_overrides.get(DECISION_EVALUATOR_ID),
                    )
                    state.decision_evaluation = evaluation.model_dump(mode="json")
                    self.repository.save(state)
                    audit = state.candidate_coverage_audit
                    await self._emit(
                        progress_callback,
                        "Decision Evaluator完了: "
                        f"positions={audit.candidate_count_position_generator if audit else 0}, "
                        f"evaluated={audit.candidate_count_evaluation if audit else 0}, "
                        f"matrix={audit.candidate_count_matrix if audit else 0}",
                    )
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
                        task_id=task_id_overrides.get(DECISION_INTEGRATOR_ID),
                        operation_variant=operation_variant,
                        model_override=model_overrides.get(DECISION_INTEGRATOR_ID),
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
                    task_id=task_id_overrides.get(QUALITY_REVIEWER_ID),
                    operation_variant=operation_variant,
                    model_override=model_overrides.get(QUALITY_REVIEWER_ID),
                )
            except Exception as exc:
                return await self._fail(
                    state,
                    f"Conclusion生成またはQuality Reviewに失敗しました: {exc}",
                    progress_callback,
                )

            state.review_result = review.model_dump(mode="json")
            self.repository.save(state)
            active_revision_finished = (
                state.revision_control.phase
                == RevisionControlPhase.EXECUTING.value
            )
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
                active_request = (
                    RevisionRequestV1.model_validate(
                        self._active_revision_request_message(state).payload
                    )
                    if state.revision_control.phase
                    == RevisionControlPhase.EXECUTING.value
                    else None
                )
                if not (
                    active_request is not None
                    and active_request.source_layer == LayerId.PLAYWRIGHT.value
                    and active_request.route == RevisionRoute.UPSTREAM.value
                ):
                    self._finalize_active_revision(
                        state,
                        review_response=response,
                        completed=True,
                        reason=review.reason,
                    )
                state.status = WorkflowStatus.WAITING_HUMAN_SELECTION
                state.current_agent_ids = []
                self.repository.save_package(package)
                self.repository.save(state)
                await self._emit(progress_callback, "Quality Gate通過。ユーザー選択待ちです")
                return state
            if review.status == QualityGateDecision.BLOCKED.value:
                self._finalize_active_revision(
                    state,
                    review_response=response,
                    completed=False,
                    reason=review.reason,
                )
                return await self._block(state, review.reason, progress_callback)
            self._finalize_active_revision(
                state,
                review_response=response,
                completed=False,
                reason=review.reason,
            )
            if review.revision_scope == RevisionScope.DELIBERATION_RETURN.value:
                return await self._request_upstream_revision(
                    state,
                    review,
                    response.message_id,
                    progress_callback,
                )
            if (
                not self.demo_safe_mode
                and state.revision_count + 1 >= self.max_revisions
            ):
                state.revision_count = min(
                    self.max_revisions,
                    state.revision_count + 1,
                )
                return await self._block(
                    state,
                    f"Quality Reviewerが{self.max_revisions}回revision_requiredを返したため停止しました",
                    progress_callback,
                )
            if self.demo_safe_mode and active_revision_finished:
                return await self._block(
                    state,
                    "Demo Safe Mode stopped before planning another paid Conclusion revision",
                    progress_callback,
                )
            effective_targets, routing_finding = (
                self._resolve_explicit_revision_targets(state, review)
            )
            self._plan_internal_revision(
                state,
                review=review,
                target_agent_ids=effective_targets,
                routing_finding=routing_finding,
            )
            self.repository.save(state)
            if self.demo_safe_mode:
                await self._emit(
                    progress_callback,
                    "Demo Safe Mode saved the Conclusion revision plan and stopped before Provider calls",
                )
                return state
            return await self._execute_upstream_refresh_revision(
                state,
                actor_id=self.agent_id,
                actor_source="SYSTEM",
                reason="Safe Mode is disabled; execute the saved Conclusion revision",
                progress_callback=progress_callback,
            )

    async def _generate_positions(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        *,
        task_id: str | None = None,
        operation_variant: str | None = None,
        model_override: str | None = None,
    ) -> PositionGenerationResult:
        task = PositionGenerationTask(
            task_id=task_id
            or self._logical_task_id(
                state,
                POSITION_GENERATOR_ID,
                operation_variant=operation_variant,
            ),
            target_agent_id=POSITION_GENERATOR_ID,
            decision_context=context,
            deliberation_result=state.deliberation_result,
            requested_candidate_count=3,
            revision_context=self._latest_revision_context(state),
        )
        result = await self._execute_agent(
            state,
            agent_id=POSITION_GENERATOR_ID,
            message_type=MessageType.POSITION_GENERATION_ASSIGNMENT,
            expected_type=MessageType.POSITION_GENERATION_RESULT,
            objective="Generate substantively distinct, traceable position candidates",
            payload=task.model_dump(mode="json"),
            output_schema=PositionGenerationResult,
            previous_stage="deliberation",
            next_stage="conclusion.decision_evaluator",
            model_override=model_override,
        )
        return result

    async def _evaluate_positions(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        generation: PositionGenerationResult,
        framework: EvaluationFramework,
        *,
        task_id: str | None = None,
        operation_variant: str | None = None,
        model_override: str | None = None,
    ) -> DecisionEvaluationResult:
        task = DecisionEvaluationTask(
            task_id=task_id
            or self._logical_task_id(
                state,
                DECISION_EVALUATOR_ID,
                operation_variant=operation_variant,
            ),
            target_agent_id=DECISION_EVALUATOR_ID,
            decision_context=context,
            position_candidates=generation.position_candidates,
            evaluation_framework=framework,
            revision_context=self._latest_revision_context(state),
        )
        result = await self._execute_agent(
            state,
            agent_id=DECISION_EVALUATOR_ID,
            message_type=MessageType.DECISION_EVALUATION_ASSIGNMENT,
            expected_type=MessageType.DECISION_EVALUATION_RESULT,
            objective="Evaluate every candidate under one non-compensatory framework",
            payload=task.model_dump(mode="json"),
            output_schema=DecisionEvaluationResult,
            previous_stage=POSITION_GENERATOR_ID,
            next_stage=DECISION_INTEGRATOR_ID,
            model_override=model_override,
        )
        audit = self._record_candidate_coverage_audit(
            state,
            task_id=result.task_id,
            payload=result.model_dump(mode="json"),
        )
        if not audit.passed:
            if DECISION_EVALUATOR_ID in state.completed_agents:
                state.completed_agents.remove(DECISION_EVALUATOR_ID)
            if DECISION_EVALUATOR_ID not in state.failed_agents:
                state.failed_agents.append(DECISION_EVALUATOR_ID)
            self.repository.save(state)
            raise ValueError(
                "Decision Evaluator candidate coverage does not match the "
                "Position Generator checkpoint"
            )
        self.repository.save(state)
        return result

    async def _integrate_decision(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        generation: PositionGenerationResult,
        evaluation: DecisionEvaluationResult,
        *,
        requested_candidate_ids: list[str],
        user_instruction: str | None,
        task_id: str | None = None,
        operation_variant: str | None = None,
        model_override: str | None = None,
    ) -> DecisionIntegrationResult:
        task = DecisionIntegrationTask(
            task_id=task_id
            or self._logical_task_id(
                state,
                DECISION_INTEGRATOR_ID,
                operation_variant=operation_variant,
            ),
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
            model_override=model_override,
        )

    async def _request_review(
        self,
        state: ConclusionWorkflowState,
        generation: PositionGenerationResult,
        evaluation: DecisionEvaluationResult,
        integration: DecisionIntegrationResult,
        package: ConclusionPackage,
        validation: DeterministicValidationResult,
        *,
        task_id: str | None = None,
        operation_variant: str | None = None,
        model_override: str | None = None,
    ) -> tuple[ConclusionQualityReviewOutput, PMPMessage]:
        state.status = WorkflowStatus.REVIEWING
        review_input = ConclusionQualityReviewInput(
            task_id=task_id
            or self._logical_task_id(
                state,
                QUALITY_REVIEWER_ID,
                operation_variant=operation_variant,
            ),
            position_generation=generation,
            decision_evaluation=evaluation,
            decision_integration=integration,
            conclusion_package=package,
            deterministic_validation=validation,
            revision_context=self._latest_revision_context(state),
        )
        saved_exchange = self._saved_stage_result_exchange(
            state,
            agent_id=QUALITY_REVIEWER_ID,
            expected_type=MessageType.CONCLUSION_QUALITY_REVIEW_RESULT,
            output_schema=ConclusionQualityReviewOutput,
            task_id=review_input.task_id,
        )
        if saved_exchange is not None:
            saved_review, saved_response = saved_exchange
            if self._stage_result_matches_state(
                state,
                DecisionContext.model_validate(state.decision_context),
                QUALITY_REVIEWER_ID,
                saved_review,
            ):
                return saved_review, saved_response
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
            model_override=model_override,
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
        model_override: str | None = None,
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
        agent = self.registry.get(agent_id)
        effective_model_override = model_override or self._compatible_model_override(
            agent_id=agent_id,
            output_schema=output_schema,
        )
        response = await agent.execute(
            request,
            model_override=effective_model_override,
        )
        state.message_history.append(response)
        state.current_agent_ids = []
        error = self._validate_response_envelope(request, response, agent_id, expected_type.value)
        if error:
            if agent_id == DECISION_EVALUATOR_ID:
                self._record_candidate_coverage_audit(
                    state,
                    task_id=str(payload.get("task_id") or "unknown_task"),
                    payload=response.payload.get("invalid_payload"),
                )
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
        if (
            isinstance(task_id, str)
            and task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX)
        ):
            self._record_verified_contract_repair(
                state,
                agent_id=agent_id,
                output_schema=output_schema,
                result_task_id=task_id,
                result_message_id=response.message_id,
            )
        return (result, response) if return_response else result

    @staticmethod
    def _unique_candidate_ids(records: Any) -> list[str]:
        if not isinstance(records, list):
            return []
        return list(
            dict.fromkeys(
                item.get("candidate_id")
                for item in records
                if isinstance(item, dict)
                and isinstance(item.get("candidate_id"), str)
                and item.get("candidate_id")
            )
        )

    def _record_candidate_coverage_audit(
        self,
        state: ConclusionWorkflowState,
        *,
        task_id: str,
        payload: Any,
        recovery_task_id: str | None = None,
    ) -> CandidateCoverageAudit:
        if (
            recovery_task_id is None
            and task_id.endswith(CANDIDATE_COVERAGE_CONTRACT_REPAIR_SUFFIX)
        ):
            recovery_task_id = task_id
        position_ids = list(
            dict.fromkeys(
                item.get("position_candidate_id")
                for item in state.position_candidates
                if isinstance(item, dict)
                and isinstance(item.get("position_candidate_id"), str)
                and item.get("position_candidate_id")
            )
        )
        raw_payload = payload if isinstance(payload, dict) else {}
        raw_evaluations = raw_payload.get("candidate_evaluations")
        raw_matrix = raw_payload.get("comparison_matrix")
        evaluation_ids = self._unique_candidate_ids(raw_evaluations)
        matrix_ids = self._unique_candidate_ids(raw_matrix)
        position_set = set(position_ids)
        evaluation_set = set(evaluation_ids)
        matrix_set = set(matrix_ids)
        missing = sorted(
            (position_set - evaluation_set) | (position_set - matrix_set)
        )
        extra = sorted(
            (evaluation_set - position_set) | (matrix_set - position_set)
        )
        evaluation_row_count = (
            len(raw_evaluations) if isinstance(raw_evaluations, list) else 0
        )
        matrix_row_count = len(raw_matrix) if isinstance(raw_matrix, list) else 0
        expected_evaluation_count = len(position_ids) * len(DEFAULT_CRITERIA)
        passed = (
            bool(position_ids)
            and evaluation_set == position_set
            and matrix_set == position_set
            and evaluation_row_count == expected_evaluation_count
            and matrix_row_count == len(position_ids)
        )
        audit = CandidateCoverageAudit(
            source_task_id=task_id,
            recovery_task_id=recovery_task_id,
            position_candidate_ids=position_ids,
            evaluation_candidate_ids=evaluation_ids,
            matrix_candidate_ids=matrix_ids,
            candidate_count_position_generator=len(position_ids),
            candidate_count_evaluation=len(evaluation_ids),
            candidate_count_matrix=len(matrix_ids),
            candidate_evaluation_row_count=evaluation_row_count,
            expected_candidate_evaluation_row_count=expected_evaluation_count,
            missing_candidate_ids=missing,
            extra_candidate_ids=extra,
            passed=passed,
        )
        state.candidate_coverage_checked = True
        state.candidate_coverage_passed = passed
        state.candidate_coverage_audit = audit
        return audit

    def _is_candidate_coverage_contract_failure(
        self,
        state: ConclusionWorkflowState,
        request: PMPMessage,
        error_response: PMPMessage,
    ) -> bool:
        if (
            request.receiver_agent_id != DECISION_EVALUATOR_ID
            or error_response.payload.get("error_class") != "PayloadValidationError"
            or "candidate_evaluations and comparison_matrix must cover the same candidates"
            not in str(error_response.payload.get("message") or "")
        ):
            return False
        payload = error_response.payload.get("invalid_payload")
        if not isinstance(payload, dict):
            return False
        audit = self._record_candidate_coverage_audit(
            state,
            task_id=str(request.payload.get("task_id") or "unknown_task"),
            payload=payload,
        )
        return not audit.passed

    @staticmethod
    def _output_schema_id(output_schema: type[BaseModel]) -> str:
        return f"{output_schema.__module__}.{output_schema.__qualname__}"

    def _compatible_model_override(
        self,
        *,
        agent_id: str,
        output_schema: type[BaseModel],
    ) -> str | None:
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        configured_model_id = getattr(agent, "model", None)
        if not isinstance(provider_id, str) or not isinstance(
            configured_model_id,
            str,
        ):
            return None
        binding = self.provider_model_compatibility_store.resolve(
            provider_id=provider_id,
            agent_id=agent_id,
            output_schema_id=self._output_schema_id(output_schema),
            configured_model_id=configured_model_id,
        )
        return binding.compatible_model_id if binding is not None else None

    def _record_verified_contract_repair(
        self,
        state: ConclusionWorkflowState,
        *,
        agent_id: str,
        output_schema: type[BaseModel],
        result_task_id: str,
        result_message_id: str,
    ) -> str:
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Provider model compatibility requires a stable provider ID")
        original_task_id = result_task_id[: -len(PROVIDER_CONTRACT_REPAIR_SUFFIX)]
        authorization = self.provider_contract_repair_store.for_original_task(
            workflow_id=state.workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Validated contract repair has no saved authorization")
        binding = self.provider_model_compatibility_store.record_verified_repair(
            authorization,
            output_schema_id=self._output_schema_id(output_schema),
            result_task_id=result_task_id,
            result_message_id=result_message_id,
        )
        return f"{binding.agent_id}: {binding.incompatible_model_id} -> {binding.compatible_model_id}"

    def _promote_saved_contract_repairs(
        self,
        state: ConclusionWorkflowState,
    ) -> list[str]:
        """Backfill verified bindings from append-only legacy PMP history."""

        responses_by_parent = {
            message.parent_message_id: message
            for message in state.message_history
            if message.parent_message_id is not None
        }
        promoted: list[str] = []
        for request in state.message_history:
            task_id = request.payload.get("task_id")
            if (
                request.sender_agent_id != self.agent_id
                or not isinstance(task_id, str)
                or not task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX)
            ):
                continue
            response = responses_by_parent.get(request.message_id)
            if (
                response is None
                or response.sender_agent_id != request.receiver_agent_id
                or response.receiver_agent_id != self.agent_id
                or response.message_type == MessageType.ERROR.value
            ):
                continue
            agent = self.registry.get(request.receiver_agent_id)
            agent.output_schema.model_validate(response.payload)
            promoted.append(
                self._record_verified_contract_repair(
                    state,
                    agent_id=request.receiver_agent_id,
                    output_schema=agent.output_schema,
                    result_task_id=task_id,
                    result_message_id=response.message_id,
                )
            )
        return list(dict.fromkeys(promoted))

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
        top_level_review_payload = handoff.payload.get("quality_review")
        top_level_review = (
            DeliberationQualityReviewOutput.model_validate(top_level_review_payload)
            if top_level_review_payload is not None
            else None
        )
        review = result.quality_review or top_level_review
        if review is None:
            raise ValueError("Deliberation Result is missing its Quality Review")
        if result.quality_review is not None and top_level_review is not None:
            if result.quality_review.model_dump(mode="json") != top_level_review.model_dump(
                mode="json"
            ):
                raise ValueError("Deliberation Quality Review copies do not match")
        if review.status not in {"approved", "approved_with_conditions"}:
            raise ValueError("Deliberation Result has not passed its Quality Gate")
        if review.conclusion_readiness not in {
            DeliberationConclusionReadiness.READY,
            DeliberationConclusionReadiness.READY_WITH_CONDITIONS,
        }:
            raise ValueError("Deliberation Result is not Conclusion-ready")
        if review.blocking_finding_ids:
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
        context = DecisionContext(
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
            human_evidence_decision=result.human_evidence_decision,
            accepted_evidence_gaps=result.accepted_evidence_gaps,
            evaluation_criteria=DEFAULT_CRITERIA,
            value_profiles=default_value_profiles(),
        )
        canonical_context, _removed_context_references = (
            self.deterministic_validator.canonical_decision_context_view(context)
        )
        return canonical_context

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
        alternatives = self._build_alternatives(
            evaluation,
            integration,
            primary_candidate_id=recommended.candidate_id if recommended else None,
        )
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
            alternatives=alternatives,
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

    @staticmethod
    def _build_alternatives(
        evaluation: DecisionEvaluationResult,
        integration: DecisionIntegrationResult,
        *,
        primary_candidate_id: str | None,
    ) -> list[dict[str, Any]]:
        """Materialize every viable non-primary option from saved Agent outputs."""

        recommendations = {
            item.candidate_id: item for item in integration.recommended_options
        }
        comparisons = {
            item.candidate_id: item.summary
            for item in integration.candidate_comparison_summary
        }
        alternatives: list[dict[str, Any]] = []
        for candidate_id in integration.viable_candidates:
            if candidate_id == primary_candidate_id:
                continue
            recommendation = recommendations.get(candidate_id)
            profile_ids: list[str] = []
            conditions: list[str] = []
            for item in evaluation.conditional_advantages:
                if candidate_id in item.advantaged_candidate_ids:
                    profile_ids.append(item.profile_id)
            for item in evaluation.sensitivity_analysis:
                if candidate_id in item.preferred_candidate_ids:
                    profile_ids.append(item.profile_id)
                    conditions.append(item.reason)
            reason = (
                recommendation.reason
                if recommendation is not None
                else comparisons.get(candidate_id)
            )
            if not reason:
                # The deterministic validator will reject this incomplete
                # materialization before Quality Review.
                reason = ""
            if not conditions and comparisons.get(candidate_id):
                conditions.append(comparisons[candidate_id])
            alternatives.append(
                {
                    "candidate_id": candidate_id,
                    "recommendation_type": (
                        recommendation.recommendation_type
                        if recommendation is not None
                        else "conditional_fallback"
                    ),
                    "reason": reason,
                    "applicable_profile_ids": list(dict.fromkeys(profile_ids)),
                    "applicable_conditions": list(dict.fromkeys(conditions)),
                }
            )
        return alternatives

    def _build_final_conclusion(
        self,
        state: ConclusionWorkflowState,
        selection: HumanSelection,
    ) -> FinalConclusion:
        package = ConclusionPackage.model_validate(state.conclusion_package)
        decision_context = DecisionContext.model_validate(state.decision_context)
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
            limitations=list(
                dict.fromkeys(
                    package.limitations
                    + selection.accepted_limitations
                    + [
                        "Human-accepted unresolved evidence gap "
                        f"{item.finding_id}: {item.issue}"
                        for item in decision_context.accepted_evidence_gaps
                    ]
                )
            ),
            human_evidence_decision=decision_context.human_evidence_decision,
            accepted_evidence_gaps=decision_context.accepted_evidence_gaps,
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

    def _send_to_playwright(
        self,
        state: ConclusionWorkflowState,
        final: FinalConclusion,
    ) -> PMPMessage:
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
            "human_evidence_decision": final_payload.get("human_evidence_decision"),
            "accepted_evidence_gaps": final_payload.get("accepted_evidence_gaps", []),
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
        return message

    async def _request_upstream_revision(
        self,
        state: ConclusionWorkflowState,
        review: ConclusionQualityReviewOutput,
        parent_message_id: str,
        progress_callback: ProgressCallback | None,
    ) -> ConclusionWorkflowState:
        requests = [item.model_dump(mode="json") for item in review.upstream_revision_requests]
        analysis_target_map = {
            "argument_analysis": "deliberation.argument_analyst",
            "causal_structural_analysis": "deliberation.causal_structural_analyst",
            "stakeholder_response_analysis": "deliberation.stakeholder_response_analyst",
            "counterargument_analysis": "deliberation.counterargument_analyst",
            "traceability_mapping": "deliberation.manager",
            "schema_validation": "deliberation.manager",
            "manager_integration": "deliberation.manager",
        }
        targets = list(
            dict.fromkeys(
                analysis_target_map.get(analysis_type, "deliberation.manager")
                for item in review.upstream_revision_requests
                for analysis_type in item.required_analysis_types
            )
        )
        source_finding_ids = list(
            dict.fromkeys(
                finding_id
                for item in review.upstream_revision_requests
                for finding_id in item.source_finding_ids
            )
        )
        revision_epoch = max(
            state.upstream_revision_count,
            state.revision_control.revision_epoch,
        ) + 1
        parent_request_id = (
            state.revision_control.active_request_id
            if state.revision_control.phase
            in {
                RevisionControlPhase.COMPLETED.value,
                RevisionControlPhase.BLOCKED.value,
            }
            else None
        )
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.CONCLUSION,
            target_layer=LayerId.DELIBERATION,
            revision_epoch=revision_epoch,
            source_review_id=parent_message_id,
            source_finding_ids=source_finding_ids,
        )
        if state.conclusion_package is None:
            raise ValueError("Conclusion upstream Revision requires a saved package")
        canonical_request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.UPSTREAM,
            source_layer=LayerId.CONCLUSION,
            target_layer=LayerId.DELIBERATION,
            revision_epoch=revision_epoch,
            root_revision_request_id=(
                state.revision_control.root_revision_request_id or request_id
                if parent_request_id
                else request_id
            ),
            parent_revision_request_id=parent_request_id,
            source_review_id=parent_message_id,
            source_finding_ids=source_finding_ids,
            target_agent_ids=targets,
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="deliberation.deliberation_result",
                    artifact_id=str(
                        state.deliberation_result["deliberation_result_id"]
                    ),
                    sha256=canonical_sha256(state.deliberation_result),
                ),
                RevisionArtifactRef(
                    artifact_type="conclusion.conclusion_package",
                    artifact_id=str(
                        state.conclusion_package["conclusion_package_id"]
                    ),
                    sha256=canonical_sha256(state.conclusion_package),
                ),
            ],
            required_actions=list(
                dict.fromkeys(
                    item.missing_analysis_description
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
            evidence_expansion_allowed=False,
            retrieval_allowed=False,
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
                    receiver_agent_id="deliberation.manager",
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Revise Deliberation analysis required for a valid Conclusion",
                    payload=canonical_request.model_dump(mode="json"),
                    constraints={
                        "new_evidence_only_if_routed_to_researcher": True,
                        "preserve_traceability": True,
                    },
                    context=PMPContext(
                        current_stage="conclusion.upstream_revision",
                        previous_stage="conclusion.quality_review",
                        next_stage="deliberation.manager",
                    ),
                    routing=PMPRouting(
                        revision_target="deliberation.manager", reply_required=True
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
                internal_limit=self.max_revisions,
                upstream_limit=self.max_revisions,
            ),
        )
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
        for saved_message in (canonical_message, message):
            if not any(
                item.message_id == saved_message.message_id
                for item in state.message_history
            ):
                state.message_history.append(saved_message)
        state.upstream_revision_count += 1
        state.upstream_revision_history.append(
            ConclusionUpstreamRevisionRecord(
                iteration=state.upstream_revision_count,
                request_message_id=message.message_id,
                requests=requests,
            )
        )
        state.status = WorkflowStatus.WAITING_UPSTREAM_REVISION
        state.revision_control = RevisionControlState(
            phase=RevisionControlPhase.WAITING_UPSTREAM_RESULT,
            revision_epoch=revision_epoch,
            active_request_id=request_id,
            active_request_message_id=canonical_message.message_id,
            root_revision_request_id=canonical_request.root_revision_request_id,
            parent_revision_request_id=canonical_request.parent_revision_request_id,
            pending_request_ids=[request_id],
            consumed_request_ids=list(state.revision_control.consumed_request_ids),
            consumed_result_ids=list(state.revision_control.consumed_result_ids),
            audit_event_ids=list(state.revision_control.audit_event_ids),
        )
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"upstream_request_written_{revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.CONCLUSION,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=canonical_message.message_id,
                artifact_ids=[
                    str(state.deliberation_result["deliberation_result_id"]),
                    str(state.conclusion_package["conclusion_package_id"]),
                ],
                reason=review.reason,
            ),
        )
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
            "human_evidence_decision", "accepted_evidence_gaps", "limitations_to_disclose",
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
        review = ConclusionQualityReviewOutput.model_validate(payload["quality_review"])
        package_review_payload = payload["conclusion_package"].get("quality_review")
        if package_review_payload is None:
            raise ValueError("Conclusion Package is missing its Quality Review")
        package_review = ConclusionQualityReviewOutput.model_validate(
            package_review_payload
        )
        if review.model_dump(mode="json") != package_review.model_dump(mode="json"):
            raise ValueError("Conclusion Quality Review copies do not match")
        if review.status not in {"approved", "approved_with_conditions"}:
            raise ValueError("Conclusion Result has not passed its Quality Gate")
        if review.playwright_readiness not in {"ready", "ready_with_conditions"}:
            raise ValueError("Conclusion Result is not Playwright-ready")
        if review.blocking_finding_ids:
            raise ValueError("Conclusion Result contains blocking findings")
        if not payload["supporting_claims"] or not payload["supporting_analysis"] or not payload["evidence_links"]:
            raise ValueError("Playwright handoff must preserve claim, analysis, and evidence traceability")
        for item in payload["evidence_links"]:
            if not item.get("evidence_id") or not item.get("source_id"):
                raise ValueError("evidence_links require evidence_id and source_id")
        final = payload["final_conclusion"]
        selection = payload["human_selection"]
        package = payload["conclusion_package"]
        trace = payload["traceability_manifest"]
        if payload["human_evidence_decision"] != final.get("human_evidence_decision"):
            raise ValueError("Human Evidence Decision copies do not match")
        if payload["accepted_evidence_gaps"] != final.get("accepted_evidence_gaps", []):
            raise ValueError("Accepted Evidence Gap copies do not match")
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
    def _saved_model_is_valid(payload: dict[str, Any] | None, schema: type[BaseModel]) -> bool:
        if payload is None:
            return False
        try:
            schema.model_validate(payload)
        except Exception:
            return False
        return True

    def _restore_saved_stage_responses(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
    ) -> dict[str, str]:
        """Restore validated result messages that outlived a checkpoint write fault."""

        stages: list[tuple[str, MessageType, type[BaseModel], str]] = [
            (
                POSITION_GENERATOR_ID,
                MessageType.POSITION_GENERATION_RESULT,
                PositionGenerationResult,
                "position_generation",
            ),
            (
                DECISION_EVALUATOR_ID,
                MessageType.DECISION_EVALUATION_RESULT,
                DecisionEvaluationResult,
                "decision_evaluation",
            ),
            (
                DECISION_INTEGRATOR_ID,
                MessageType.DECISION_INTEGRATION_RESULT,
                DecisionIntegrationResult,
                "decision_integration",
            ),
            (
                QUALITY_REVIEWER_ID,
                MessageType.CONCLUSION_QUALITY_REVIEW_RESULT,
                ConclusionQualityReviewOutput,
                "review_result",
            ),
        ]
        restored: dict[str, str] = {}
        for agent_id, expected_type, output_schema, state_field in stages:
            if self._saved_model_is_valid(getattr(state, state_field), output_schema):
                continue
            task_id = self._logical_task_id(state, agent_id)
            exchange = None
            for candidate_task_id in reversed(
                self._recoverable_stage_task_ids(state, agent_id)
            ):
                exchange = self._saved_stage_result_exchange(
                    state,
                    agent_id=agent_id,
                    expected_type=expected_type,
                    output_schema=output_schema,
                    task_id=candidate_task_id,
                )
                if exchange is None:
                    exchange = self._recover_saved_reference_invalid_stage(
                        state,
                        context,
                        agent_id=agent_id,
                        expected_type=expected_type,
                        output_schema=output_schema,
                        task_id=candidate_task_id,
                    )
                if exchange is not None:
                    task_id = candidate_task_id
                    break
            if exchange is None:
                break
            stage_result, _response = exchange
            if not self._stage_result_matches_state(
                state,
                context,
                agent_id,
                stage_result,
            ):
                break
            if self._stage_reference_violations(
                state,
                context,
                agent_id,
                stage_result,
            ):
                break
            if agent_id == POSITION_GENERATOR_ID:
                state.position_generation = stage_result.model_dump(mode="json")
                state.position_candidates = [
                    item.model_dump(mode="json")
                    for item in stage_result.position_candidates
                ]
            elif agent_id == DECISION_EVALUATOR_ID:
                state.decision_evaluation = stage_result.model_dump(mode="json")
                state.evaluation_framework = stage_result.evaluation_framework.model_dump(
                    mode="json"
                )
            elif agent_id == DECISION_INTEGRATOR_ID:
                state.decision_integration = stage_result.model_dump(mode="json")
            else:
                state.review_result = stage_result.model_dump(mode="json")
            if agent_id not in state.completed_agents:
                state.completed_agents.append(agent_id)
            restored[agent_id] = task_id
        return restored

    def _recover_saved_reference_invalid_stage(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        *,
        agent_id: str,
        expected_type: MessageType,
        output_schema: type[BaseModel],
        task_id: str,
    ) -> tuple[BaseModel, PMPMessage] | None:
        """Promote a billed payload after lossless reference-list filtering.

        Only unknown elements of explicit structured reference arrays may be
        removed. Scalar references, narrative values, schema-invalid payloads,
        missing reservations, and payloads that become incomplete are never
        repaired here and remain eligible only for an explicit audited retry.
        """

        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", task_id):
            return None
        requests = {
            message.message_id: message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == agent_id
            and message.payload.get("task_id") == task_id
        }
        if not requests:
            return None
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            return None
        reservation_path = (
            self.repository.data_dir
            / "provider_call_reservations"
            / provider_id
            / state.workflow_id
            / f"{task_id}.json"
        )
        try:
            reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if (
            reservation.get("workflow_id") != state.workflow_id
            or reservation.get("task_id") != task_id
            or reservation.get("agent_id") != agent_id
        ):
            return None

        for response in reversed(state.message_history):
            if (
                response.sender_agent_id != agent_id
                or response.receiver_agent_id != self.agent_id
                or response.message_type != MessageType.ERROR.value
                or response.parent_message_id not in requests
                or not self._is_persisted_provider_output_validation_error(response)
                or reservation.get("model_id") != response.payload.get("model_id")
            ):
                continue
            validation_errors = response.payload.get("validation_errors")
            if any(
                not isinstance(item, dict)
                or item.get("type") != "value_error.reference_integrity"
                for item in validation_errors
            ):
                continue
            raw = response.payload.get("invalid_payload")
            try:
                raw_result = output_schema.model_validate(raw)
            except Exception:
                continue
            violations = self._stage_reference_violations(
                state,
                context,
                agent_id,
                raw_result,
            )
            declared_paths = {
                str(item.get("loc"))
                for item in validation_errors
                if isinstance(item.get("loc"), str)
            }
            if not violations or declared_paths != {
                item["path"] for item in violations
            }:
                continue
            normalized = self._remove_unknown_reference_list_items(raw, violations)
            if normalized is None:
                continue
            try:
                result = output_schema.model_validate(normalized)
            except Exception:
                continue
            if getattr(result, "task_id", task_id) != task_id:
                continue
            if not self._stage_result_matches_state(
                state,
                context,
                agent_id,
                result,
            ):
                continue
            if self._stage_reference_violations(
                state,
                context,
                agent_id,
                result,
            ):
                continue

            request = requests[response.parent_message_id]
            recovery_record = {
                "task_id": task_id,
                "agent_id": agent_id,
                "source_error_message_id": response.message_id,
                "provider_call_reused": False,
                "compatibility_adapter": "drop_unknown_structured_reference_list_items",
                "removed_references": violations,
                "recovered_at": utc_now().isoformat(),
            }
            extensions = dict(response.metadata.extensions)
            extensions["provider_payload_recovery"] = recovery_record
            recovered_response = PMPMessage.create(
                workflow_id=state.workflow_id,
                parent_message_id=request.message_id,
                sender_agent_id=agent_id,
                receiver_agent_id=self.agent_id,
                message_type=expected_type,
                objective=(
                    "Recover persisted Provider output after canonical reference "
                    "list validation"
                ),
                payload=result.model_dump(mode="json"),
                constraints=request.constraints,
                context=PMPContext(
                    current_stage=agent_id,
                    previous_stage=request.context.current_stage,
                    next_stage=self.agent_id,
                ),
                metadata=PMPMetadata(
                    status=MessageStatus.COMPLETED,
                    retry_count=response.metadata.retry_count,
                    notes=(
                        "Recovered from persisted invalid_payload without a Provider call; "
                        "removed only unknown structured reference list elements"
                    ),
                    extensions=extensions,
                ),
            )
            self.pmp_validator.validate(recovered_response)
            state.message_history.append(recovered_response)
            if not any(
                item.get("source_error_message_id") == response.message_id
                for item in state.provider_payload_recoveries
            ):
                state.provider_payload_recoveries.append(recovery_record)
            return result, recovered_response
        return None

    def _stage_reference_violations(
        self,
        state: ConclusionWorkflowState,
        context: DecisionContext,
        agent_id: str,
        result: BaseModel,
    ) -> list[dict[str, str]]:
        if agent_id == POSITION_GENERATOR_ID:
            candidate_ids = {
                item.position_candidate_id
                for item in getattr(result, "position_candidates", [])
            }
        else:
            candidate_ids = {
                item.get("position_candidate_id")
                for item in state.position_candidates
                if isinstance(item, dict) and item.get("position_candidate_id")
            }
        return self.deterministic_validator.unknown_reference_ids(
            decision_context=context,
            value=result,
            candidate_ids=candidate_ids,
        )

    @staticmethod
    def _remove_unknown_reference_list_items(
        raw: Any,
        violations: list[dict[str, str]],
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        normalized = deepcopy(raw)
        grouped: dict[tuple[str | int, ...], list[tuple[int, str]]] = {}
        for violation in violations:
            path = violation.get("path")
            expected = violation.get("id")
            if not isinstance(path, str) or not isinstance(expected, str):
                return None
            parts: list[str | int] = []
            for match in re.finditer(r"([^.\[\]]+)|\[(\d+)\]", path):
                name, index = match.groups()
                parts.append(int(index) if index is not None else name)
            if not parts or not isinstance(parts[-1], int):
                return None
            grouped.setdefault(tuple(parts[:-1]), []).append((parts[-1], expected))

        for parent_path, items in grouped.items():
            parent: Any = normalized
            try:
                for part in parent_path:
                    parent = parent[part]
            except (KeyError, IndexError, TypeError):
                return None
            if not isinstance(parent, list):
                return None
            for index, expected in sorted(items, reverse=True):
                if index >= len(parent) or parent[index] != expected:
                    return None
                parent.pop(index)
        return normalized

    def _saved_stage_result_exchange(
        self,
        state: ConclusionWorkflowState,
        *,
        agent_id: str,
        expected_type: MessageType,
        output_schema: type[BaseModel],
        task_id: str,
    ) -> tuple[BaseModel, PMPMessage] | None:
        requests = {
            message.message_id: message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == agent_id
            and message.payload.get("task_id") == task_id
        }
        for response in reversed(state.message_history):
            if (
                response.sender_agent_id != agent_id
                or response.receiver_agent_id != self.agent_id
                or response.message_type != expected_type.value
                or response.parent_message_id not in requests
            ):
                continue
            request = requests[response.parent_message_id]
            if self._validate_response_envelope(
                request,
                response,
                agent_id,
                expected_type.value,
            ):
                continue
            try:
                result = output_schema.model_validate(response.payload)
            except Exception:
                continue
            result_task_id = getattr(result, "task_id", task_id)
            if result_task_id != task_id:
                continue
            return result, response
        return None

    @staticmethod
    def _stage_result_matches_state(
        state: ConclusionWorkflowState,
        context: DecisionContext,
        agent_id: str,
        result: BaseModel,
    ) -> bool:
        if agent_id == POSITION_GENERATOR_ID:
            return result.decision_context_id == context.decision_context_id
        if agent_id == DECISION_EVALUATOR_ID:
            candidate_ids = {
                item["position_candidate_id"] for item in state.position_candidates
            }
            reviewed_ids = {
                item.candidate_id for item in result.comparison_matrix
            }
            return (
                result.decision_context_id == context.decision_context_id
                and reviewed_ids == candidate_ids
            )
        if agent_id == DECISION_INTEGRATOR_ID:
            if not state.decision_evaluation:
                return False
            return (
                result.decision_evaluation_result_id
                == state.decision_evaluation.get("decision_evaluation_result_id")
            )
        if not state.position_generation or not state.decision_evaluation or not state.decision_integration:
            return False
        return (
            set(result.reviewed_candidate_ids)
            == {
                item["position_candidate_id"] for item in state.position_candidates
            }
            and result.reviewed_evaluation_result_id
            == state.decision_evaluation.get("decision_evaluation_result_id")
            and result.reviewed_integration_result_id
            == state.decision_integration.get("decision_integration_result_id")
        )

    def _has_unanswered_stage_request(
        self,
        state: ConclusionWorkflowState,
        agent_id: str,
    ) -> bool:
        task_ids = set(self._recoverable_stage_task_ids(state, agent_id))
        requests = [
            message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == agent_id
            and message.payload.get("task_id") in task_ids
        ]
        if not requests:
            return False
        child_parent_ids = {
            message.parent_message_id
            for message in state.message_history
            if message.parent_message_id is not None
        }
        return any(request.message_id not in child_parent_ids for request in requests)

    @staticmethod
    def _has_unanswered_task_request(
        state: ConclusionWorkflowState,
        agent_id: str,
        task_id: str,
    ) -> bool:
        requests = [
            message
            for message in state.message_history
            if message.sender_agent_id == "conclusion.manager"
            and message.receiver_agent_id == agent_id
            and message.payload.get("task_id") == task_id
        ]
        child_parent_ids = {
            message.parent_message_id
            for message in state.message_history
            if message.parent_message_id is not None
        }
        return any(request.message_id not in child_parent_ids for request in requests)

    def _recoverable_stage_task_ids(
        self,
        state: ConclusionWorkflowState,
        agent_id: str,
    ) -> list[str]:
        base_task_id = self._logical_task_id(state, agent_id)
        task_ids = [base_task_id]
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            return task_ids
        retry = self.provider_retry_store.for_original_task(
            workflow_id=state.workflow_id,
            provider_id=provider_id,
            original_task_id=base_task_id,
        )
        if retry is not None:
            task_ids.append(retry.retry_task_id)
        repair = self.provider_contract_repair_store.for_original_task(
            workflow_id=state.workflow_id,
            provider_id=provider_id,
            original_task_id=base_task_id,
        )
        if repair is not None:
            task_ids.append(repair.repair_task_id)
        # Random legacy task IDs predate revision-cycle identity. They may
        # restore an initial checkpoint, but must never satisfy a later
        # revision/upstream checkpoint merely because the Decision Context is
        # unchanged.
        if state.revision_count or state.upstream_revision_count:
            return task_ids
        # Legacy Conclusion checkpoints used random stage task IDs. Only admit
        # their explicitly authorized retry/repair descendants here; arbitrary
        # historical stage requests remain ineligible for checkpoint restore.
        for message in state.message_history:
            if (
                message.sender_agent_id != self.agent_id
                or message.receiver_agent_id != agent_id
            ):
                continue
            task_id = message.payload.get("task_id")
            if not isinstance(task_id, str):
                continue
            if task_id.endswith(OPERATOR_RETRY_SUFFIX):
                legacy_base = task_id[: -len(OPERATOR_RETRY_SUFFIX)]
                legacy_retry = self.provider_retry_store.for_original_task(
                    workflow_id=state.workflow_id,
                    provider_id=provider_id,
                    original_task_id=legacy_base,
                )
                if legacy_retry is not None and legacy_retry.retry_task_id == task_id:
                    for candidate in (legacy_base, task_id):
                        if candidate not in task_ids:
                            task_ids.append(candidate)
            elif task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX):
                legacy_base = task_id[: -len(PROVIDER_CONTRACT_REPAIR_SUFFIX)]
                legacy_repair = (
                    self.provider_contract_repair_store.for_original_task(
                        workflow_id=state.workflow_id,
                        provider_id=provider_id,
                        original_task_id=legacy_base,
                    )
                )
                if (
                    legacy_repair is not None
                    and legacy_repair.repair_task_id == task_id
                ):
                    for candidate in (
                        legacy_base,
                        legacy_repair.retry_task_id,
                        task_id,
                    ):
                        if candidate not in task_ids:
                            task_ids.append(candidate)
        return task_ids

    @staticmethod
    def _is_legacy_non_finite_root_error(error_response: PMPMessage) -> bool:
        message = str(error_response.payload.get("message") or "").lower()
        return (
            error_response.payload.get("error_class") == "PayloadValidationError"
            and error_response.payload.get("validation_field_path") is None
            and "input_type=float" in message
            and any(
                marker in message
                for marker in ("input_value=inf", "input_value=-inf", "input_value=nan")
            )
        )

    @staticmethod
    def _is_persisted_provider_output_validation_error(
        error_response: PMPMessage,
    ) -> bool:
        """Recognize a billed Provider result that failed local output validation.

        The invalid payload and structured validation errors are required so a
        generic application-side validation bug cannot authorize a Provider call.
        Request/error correlation and the original reservation are checked by
        the caller and ProviderRetryAuthorizationStore respectively.
        """

        validation_errors = error_response.payload.get("validation_errors")
        return (
            error_response.payload.get("error_class") == "PayloadValidationError"
            and isinstance(error_response.payload.get("invalid_payload"), dict)
            and isinstance(validation_errors, list)
            and bool(validation_errors)
            and isinstance(error_response.payload.get("model_id"), str)
            and bool(error_response.payload.get("model_id"))
        )

    @staticmethod
    def _latest_failed_provider_exchange(
        state: ConclusionWorkflowState,
    ) -> tuple[PMPMessage, PMPMessage]:
        conclusion_agents = {
            POSITION_GENERATOR_ID,
            DECISION_EVALUATOR_ID,
            DECISION_INTEGRATOR_ID,
            QUALITY_REVIEWER_ID,
        }
        for error_response in reversed(state.message_history):
            if (
                error_response.message_type != MessageType.ERROR.value
                or error_response.sender_agent_id not in conclusion_agents
            ):
                continue
            request = next(
                (
                    message
                    for message in reversed(state.message_history)
                    if message.message_id == error_response.parent_message_id
                ),
                None,
            )
            if (
                request is None
                or request.sender_agent_id != "conclusion.manager"
                or request.receiver_agent_id != error_response.sender_agent_id
                or request.payload.get("task_id")
                != error_response.payload.get("task_id")
            ):
                raise ValueError(
                    "Conclusion Provider failure is not correlated to its saved request"
                )
            return request, error_response
        raise ValueError("No failed Conclusion Provider exchange was found")

    @staticmethod
    def _failed_provider_exchange_for_task(
        state: ConclusionWorkflowState,
        task_id: str,
    ) -> tuple[PMPMessage, PMPMessage] | None:
        for error_response in reversed(state.message_history):
            if (
                error_response.message_type != MessageType.ERROR.value
                or error_response.payload.get("task_id") != task_id
            ):
                continue
            request = next(
                (
                    message
                    for message in reversed(state.message_history)
                    if message.message_id == error_response.parent_message_id
                ),
                None,
            )
            if (
                request is None
                or request.sender_agent_id != "conclusion.manager"
                or request.receiver_agent_id != error_response.sender_agent_id
                or request.payload.get("task_id") != task_id
            ):
                raise ValueError(
                    "Conclusion Provider failure is not correlated to its saved request"
                )
            return request, error_response
        return None

    @staticmethod
    def _clear_from_failed_stage(
        state: ConclusionWorkflowState,
        failed_agent_id: str,
    ) -> None:
        order = [
            POSITION_GENERATOR_ID,
            DECISION_EVALUATOR_ID,
            DECISION_INTEGRATOR_ID,
            QUALITY_REVIEWER_ID,
        ]
        start = order.index(failed_agent_id)
        invalidated_agents = set(order[start:])
        state.completed_agents = [
            agent_id
            for agent_id in state.completed_agents
            if agent_id not in invalidated_agents
        ]
        if failed_agent_id == POSITION_GENERATOR_ID:
            state.position_generation = None
            state.position_candidates = []
            state.evaluation_framework = None
            state.candidate_coverage_checked = False
            state.candidate_coverage_passed = False
            state.candidate_coverage_audit = None
        if failed_agent_id in {POSITION_GENERATOR_ID, DECISION_EVALUATOR_ID}:
            state.decision_evaluation = None
        if failed_agent_id in {
            POSITION_GENERATOR_ID,
            DECISION_EVALUATOR_ID,
            DECISION_INTEGRATOR_ID,
        }:
            state.decision_integration = None
        state.conclusion_package = None
        state.deterministic_validation = None
        state.review_result = None
        state.human_selection = None
        state.final_conclusion = None
        state.playwright_sent = False

    @staticmethod
    def _logical_task_id(
        state: ConclusionWorkflowState,
        agent_id: str,
        *,
        operation_variant: str | None = None,
    ) -> str:
        stage_names = {
            POSITION_GENERATOR_ID: "position_generation",
            DECISION_EVALUATOR_ID: "decision_evaluation",
            DECISION_INTEGRATOR_ID: "decision_integration",
            QUALITY_REVIEWER_ID: "quality_review",
        }
        stage_name = stage_names[agent_id]
        task_id = (
            f"conclusion_{stage_name}_upstream_{state.upstream_revision_count}"
            f"_revision_{state.revision_count}"
        )
        if operation_variant:
            task_id += f"_{operation_variant}"
        current_manager_repairs = [
            record
            for record in state.manager_repair_history
            if record.upstream_revision_count == state.upstream_revision_count
            and record.revision_count == state.revision_count
        ]
        if current_manager_repairs:
            task_id += f"_manager_repair_{current_manager_repairs[-1].iteration}"
        return task_id

    def _record_revision_audit(
        self,
        state: ConclusionWorkflowState,
        event: RevisionAuditEvent,
    ) -> None:
        self.revision_exchange.create_audit_event_once(event)
        if event.audit_event_id not in state.revision_control.audit_event_ids:
            state.revision_control.audit_event_ids.append(event.audit_event_id)

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
