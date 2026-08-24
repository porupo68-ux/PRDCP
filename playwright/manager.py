from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
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
from common.provider_capability_repair import (
    PROVIDER_CAPABILITY_REPAIR_SUFFIX,
    ProviderCapabilityRepairAuthorization,
    ProviderCapabilityRepairAuthorizationStore,
)
from common.provider_model_compatibility import ProviderModelCompatibilityStore
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from common.provider_retry import (
    OPERATOR_RETRY_SUFFIX,
    ProviderRetryAuthorization,
    ProviderRetryAuthorizationStore,
)
from common.validation import PMPValidator
from conclusion.schemas.review import ConclusionQualityReviewOutput
from playwright.deterministic_repair import PlaywrightDeterministicRepairer
from playwright.registry import PlaywrightRegistry
from playwright.schemas import (
    CitationEditingResult,
    CitationEditingTask,
    CitationManifest,
    CitationValidatedScript,
    DeterministicValidationResult,
    FinalScriptPackage,
    NarrativeBlueprint,
    NarrativeDesignTask,
    PlaywrightFinalGateResult,
    PlaywrightGateStatus,
    ProductionContext,
    ScriptDraft,
    ScriptWritingTask,
    UpstreamConclusionRevisionRequest,
    ValidationSeverity,
    VisualDirectionTask,
    VisualPlan,
)
from playwright.schemas.citation_manifest import DisclosureCheck
from playwright.state import (
    PlaywrightRevisionRecord,
    PlaywrightStatus,
    PlaywrightUpstreamRevisionRecord,
    PlaywrightWorkflowState,
    utc_now,
)
from playwright.validator import (
    PlaywrightValidator,
    canonical_hash,
    canonical_script_claim_ids,
)
from playwright.workflow import (
    AGENT_ORDER,
    EVIDENCE_CITATION_EDITOR_ID,
    NARRATIVE_ARCHITECT_ID,
    REVISION_DEPENDENCIES,
    SCRIPTWRITER_ID,
    VISUAL_DIRECTOR_ID,
)
from storage.playwright_workflow_repository import PlaywrightWorkflowRepository
from storage.revision_exchange_repository import (
    RevisionBudgetExhausted,
    RevisionExchangeRepository,
)


ProgressCallback = Callable[[str], Awaitable[None]]


class PlaywrightManager:
    agent_id = "playwright.manager"

    def __init__(
        self,
        registry: PlaywrightRegistry,
        repository: PlaywrightWorkflowRepository,
        *,
        max_revisions: int = 2,
        target_duration_seconds: int = 720,
        target_audience: str = "一般の成人視聴者",
        video_format: str = "YouTube解説動画",
        language: str = "ja",
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
        # Safe Mode blocks automatic transition into a revision cycle.  Keep
        # the configured limit so an explicit operator command can consume one
        # audited cycle without enabling a loop.
        self.max_revisions = max_revisions
        self.target_duration_seconds = target_duration_seconds
        self.target_audience = target_audience
        self.video_format = video_format
        self.language = language
        self.rd_loader = rd_loader or registry.rd_loader
        self.pmp_validator = PMPValidator()
        self.validator = PlaywrightValidator()
        self.deterministic_repairer = PlaywrightDeterministicRepairer()
        self.provider_retry_store = ProviderRetryAuthorizationStore(repository.data_dir)
        self.provider_capability_repair_store = (
            ProviderCapabilityRepairAuthorizationStore(repository.data_dir)
        )
        self.provider_model_compatibility_store = ProviderModelCompatibilityStore(
            repository.data_dir
        )
        self.revision_exchange = RevisionExchangeRepository(repository.data_dir)

    def authorize_provider_retry(
        self,
        workflow_id: str,
    ) -> ProviderRetryAuthorization:
        """Authorize one explicit retry of the latest failed Playwright task."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Operator provider retry is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != PlaywrightStatus.FAILED.value:
            raise ValueError("Playwright must be FAILED before an operator provider retry")
        request, error_response = self._latest_failed_provider_exchange(state)
        original_task_id = self._request_task_id(request)
        if original_task_id.endswith(OPERATOR_RETRY_SUFFIX):
            raise ValueError("The one-time Playwright operator retry is exhausted")
        error_class = self._retryable_error_class(error_response)
        if error_class is None:
            raise ValueError(
                "Latest Playwright failure is not eligible for an explicit Provider retry"
            )
        agent = self.registry.get(request.receiver_agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Playwright Provider has no stable logical provider ID")
        return self.provider_retry_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=request.receiver_agent_id,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            source_error_class=error_class,
        )

    async def retry_provider_call(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
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

    def authorize_provider_capability_repair(
        self,
        workflow_id: str,
        *,
        repair_model_id: str,
    ) -> ProviderCapabilityRepairAuthorization:
        """Authorize one different-model repair for an incapable endpoint."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Provider capability repair is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != PlaywrightStatus.FAILED.value:
            raise ValueError(
                "Playwright must be FAILED before a provider capability repair"
            )
        request, error_response = self._latest_failed_provider_exchange(state)
        original_task_id = self._request_task_id(request)
        if original_task_id.endswith(
            (OPERATOR_RETRY_SUFFIX, PROVIDER_CAPABILITY_REPAIR_SUFFIX)
        ):
            raise ValueError(
                "Provider capability repair requires the original failed task"
            )
        if not self._is_provider_capability_failure(error_response):
            raise ValueError(
                "Provider capability repair requires the saved OpenRouter endpoint-capability 404"
            )
        agent = self.registry.get(request.receiver_agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Playwright Provider has no stable logical provider ID")
        failed_model_id = str(error_response.payload.get("model_id") or "").strip()
        repair_model_id = repair_model_id.strip()
        if not failed_model_id:
            raise ValueError("Failed Playwright response has no model identity")
        return self.provider_capability_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=request.receiver_agent_id,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            source_error_class=str(error_response.payload.get("error_class") or ""),
            source_http_status=int(error_response.payload.get("http_status") or 0),
            failed_model_id=failed_model_id,
            repair_model_id=repair_model_id,
        )

    async def repair_provider_capability(
        self,
        workflow_id: str,
        *,
        repair_model_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        authorization = self.authorize_provider_capability_repair(
            workflow_id,
            repair_model_id=repair_model_id,
        )
        await self._emit(
            progress_callback,
            "One-time provider capability repair authorized: "
            + authorization.repair_task_id
            + " -> "
            + authorization.repair_model_id,
        )
        return await self.recover(
            workflow_id,
            progress_callback=progress_callback,
        )

    async def revise(
        self,
        workflow_id: str,
        *,
        actor_id: str = "cli.operator",
        actor_source: str = "CLI",
        reason: str = "Operator authorized Playwright revision",
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        """Run one operator-authorized internal Playwright revision in Safe Mode."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Explicit Playwright revision is available only while Demo Safe Mode "
                "is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != PlaywrightStatus.BLOCKED.value:
            raise ValueError(
                "Playwright must be BLOCKED at a saved deterministic gate before "
                "an explicit revision"
            )
        if (
            state.revision_control.phase
            == RevisionControlPhase.AUTHORIZATION_REQUIRED.value
        ):
            return await self._execute_active_revision(
                state,
                actor_id=actor_id,
                actor_source=actor_source,
                reason=reason,
                progress_callback=progress_callback,
            )
        if (
            state.deterministic_validation is None
            or state.final_gate_result is None
        ):
            raise ValueError(
                "Playwright has no complete saved deterministic gate checkpoint"
            )
        validation = DeterministicValidationResult.model_validate(
            state.deterministic_validation
        )
        # Rebuild the decision so checkpoints produced before Safe Mode kept
        # the configured revision limit remain readable without rewriting the
        # saved JSON in place.
        gate = self._final_gate(state, validation)
        if gate.status != PlaywrightGateStatus.REVISION_REQUIRED.value:
            raise ValueError(
                "Explicit Playwright revision requires repairable internal findings"
            )
        targets = list(dict.fromkeys(gate.revision_targets))
        if not targets or any(target not in AGENT_ORDER for target in targets):
            raise ValueError(
                "Playwright revision findings do not resolve to internal Agent targets"
            )
        self._plan_internal_revision(state, gate)
        self.repository.save(state)
        return await self._execute_active_revision(
            state,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            progress_callback=progress_callback,
        )

    def _plan_internal_revision(
        self,
        state: PlaywrightWorkflowState,
        gate: PlaywrightFinalGateResult,
    ) -> PMPMessage:
        targets = list(dict.fromkeys(gate.revision_targets))
        if not targets or any(item not in AGENT_ORDER for item in targets):
            raise ValueError("Playwright Revision has no executable target")
        finding_ids = list(
            dict.fromkeys(
                str(item.get("finding_id") or "") for item in gate.findings
            )
        )
        finding_ids = [item for item in finding_ids if item]
        if not finding_ids:
            raise ValueError("Playwright Revision requires correlated findings")
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
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.PLAYWRIGHT,
            target_layer=LayerId.PLAYWRIGHT,
            revision_epoch=epoch,
            source_review_id=gate.final_gate_result_id,
            source_finding_ids=finding_ids,
        )
        request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.INTERNAL,
            source_layer=LayerId.PLAYWRIGHT,
            target_layer=LayerId.PLAYWRIGHT,
            revision_epoch=epoch,
            root_revision_request_id=(
                state.revision_control.root_revision_request_id or request_id
                if parent_request_id
                else request_id
            ),
            parent_revision_request_id=parent_request_id,
            source_review_id=gate.final_gate_result_id,
            source_finding_ids=finding_ids,
            target_agent_ids=targets,
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="conclusion.final_conclusion",
                    artifact_id=str(state.final_conclusion["final_conclusion_id"]),
                    sha256=canonical_sha256(state.final_conclusion),
                ),
                RevisionArtifactRef(
                    artifact_type="playwright.deterministic_validation",
                    artifact_id=str(
                        state.deterministic_validation["validation_id"]
                    ),
                    sha256=canonical_sha256(state.deterministic_validation),
                ),
            ],
            required_actions=list(
                dict.fromkeys(
                    str(item.get("message") or item.get("code") or "repair")
                    for item in gate.findings
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
        first_agent = min(targets, key=AGENT_ORDER.index)
        message_id = str(uuid5(NAMESPACE_URL, f"{request_id}:request-message"))
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=state.message_history[-1].message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id=NARRATIVE_ARCHITECT_ID,
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Run the minimum Playwright dependency closure required by Final Gate",
                    payload=request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="playwright.revision_planned",
                        previous_stage="playwright.final_gate",
                        next_stage=first_agent,
                    ),
                    routing=PMPRouting(
                        revision_target=NARRATIVE_ARCHITECT_ID,
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
        state.status = PlaywrightStatus.BLOCKED
        state.error = {
            "code": "REVISION_AUTHORIZATION_REQUIRED",
            "message": "Playwright Revision plan is saved and requires authorization",
        }
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"request_written_{epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.PLAYWRIGHT,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=message.message_id,
                artifact_ids=[item.artifact_id for item in request.base_artifacts],
                reason=gate.delivery_readiness,
            ),
        )
        return message

    async def _execute_active_revision(
        self,
        state: PlaywrightWorkflowState,
        *,
        actor_id: str,
        actor_source: str,
        reason: str,
        progress_callback: ProgressCallback | None,
    ) -> PlaywrightWorkflowState:
        if state.revision_count >= self.max_revisions:
            raise ValueError(
                f"Playwright revision limit {self.max_revisions} is exhausted"
            )
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        if request.route != RevisionRoute.INTERNAL.value:
            raise ValueError("Playwright execution accepts only internal Revision")
        self.revision_exchange.validator.validate_current_base_artifacts(
            request,
            {
                (
                    "conclusion.final_conclusion",
                    str(state.final_conclusion.get("final_conclusion_id") or ""),
                ): canonical_sha256(state.final_conclusion),
                (
                    "playwright.deterministic_validation",
                    str(
                        (state.deterministic_validation or {}).get("validation_id")
                        or ""
                    ),
                ): canonical_sha256(state.deterministic_validation),
            },
        )
        provider_tasks = self._revision_execution_identities(state, request)
        authorization = RevisionExecutionAuthorization(
            authorization_id=(
                "revision_authorization_"
                + uuid5(NAMESPACE_URL, request.revision_request_id).hex
            ),
            workflow_id=state.workflow_id,
            revision_request_id=request.revision_request_id,
            executing_layer=LayerId.PLAYWRIGHT,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            max_provider_calls=len(provider_tasks),
            max_retrieval_calls=0,
        )
        try:
            current_authorization = self.revision_exchange.load_authorization(
                executing_layer=LayerId.PLAYWRIGHT,
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
            )
            if (
                current_authorization.actor_id != actor_id
                or current_authorization.actor_source != actor_source
                or current_authorization.reason != reason
            ):
                raise ValueError(
                    "Playwright Revision is already authorized by a different actor"
                )
            authorization = current_authorization
        except FileNotFoundError:
            self.revision_exchange.create_authorization_once(authorization)
            self._record_revision_audit(
                state,
                RevisionAuditEvent(
                    audit_event_id=f"authorization_created_{request.revision_epoch}",
                    workflow_id=state.workflow_id,
                    revision_request_id=request.revision_request_id,
                    layer=LayerId.PLAYWRIGHT,
                    event_type=RevisionAuditEventType.AUTHORIZATION_CREATED,
                    actor_id=actor_id,
                    reason=reason,
                ),
            )
        try:
            budget = self.revision_exchange.budget_store.consume(
                policy=RevisionBudgetPolicy(
                    internal_limit=self.max_revisions,
                    upstream_limit=self.max_revisions,
                ),
                workflow_id=state.workflow_id,
                layer=LayerId.PLAYWRIGHT,
                route=RevisionRoute.INTERNAL,
                revision_request_id=request.revision_request_id,
            )
        except RevisionBudgetExhausted as exc:
            state.revision_control.phase = RevisionControlPhase.BLOCKED
            state.status = PlaywrightStatus.BLOCKED
            state.error = {"code": "REVISION_BUDGET_EXHAUSTED", "message": str(exc)}
            self._record_revision_audit(
                state,
                RevisionAuditEvent(
                    audit_event_id=f"budget_blocked_{request.revision_epoch}",
                    workflow_id=state.workflow_id,
                    revision_request_id=request.revision_request_id,
                    layer=LayerId.PLAYWRIGHT,
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
                layer=LayerId.PLAYWRIGHT,
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
                layer=LayerId.PLAYWRIGHT,
                event_type=RevisionAuditEventType.BUDGET_CONSUMED,
                actor_id=self.agent_id,
                reason=f"Playwright internal revision slot {budget.iteration}",
                created_at=budget.consumed_at,
            ),
            RevisionAuditEvent(
                audit_event_id=f"request_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PLAYWRIGHT,
                event_type=RevisionAuditEventType.REQUEST_CONSUMED,
                actor_id=self.agent_id,
                message_id=request_message.message_id,
                reason="Playwright Manager began the saved revision plan",
                created_at=budget.consumed_at,
            ),
        ):
            self._record_revision_audit(state, event)
        state.revision_count = max(state.revision_count, budget.iteration)
        rerun_from = min(
            AGENT_ORDER.index(item) for item in request.target_agent_ids
        )
        if not any(
            record.iteration == budget.iteration for record in state.revision_history
        ):
            state.revision_history.append(
                PlaywrightRevisionRecord(
                    iteration=budget.iteration,
                    target_agent_ids=request.target_agent_ids,
                    findings=[
                        {"finding_id": item, "source": request.source_review_id}
                        for item in request.source_finding_ids
                    ],
                    rerun_stages=REVISION_DEPENDENCIES[AGENT_ORDER[rerun_from]],
                )
            )
        self._clear_from(state, rerun_from)
        state.revision_control.phase = RevisionControlPhase.EXECUTING
        state.status = PlaywrightStatus.REVISING
        state.error = None
        state.current_agent_ids = []
        self.repository.save(state)
        await self._emit(
            progress_callback,
            "Authorized Playwright Revision → "
            + ", ".join(request.target_agent_ids)
            + f"（{budget.iteration}/{self.max_revisions}）",
        )
        return await self._run(
            state,
            rerun_from=rerun_from,
            task_id_overrides=provider_tasks,
            progress_callback=progress_callback,
        )

    def _active_revision_request_message(
        self,
        state: PlaywrightWorkflowState,
    ) -> PMPMessage:
        request_id = state.revision_control.active_request_id
        message_id = state.revision_control.active_request_message_id
        if not request_id or not message_id:
            raise ValueError("Playwright has no active Revision Request")
        message = next(
            (item for item in state.message_history if item.message_id == message_id),
            None,
        )
        if message is None:
            message = self.revision_exchange.load_internal_request(
                layer=LayerId.PLAYWRIGHT,
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
            )
        request = self.revision_exchange.validator.validate_request_message(message)
        if request.revision_request_id != request_id:
            raise ValueError("Playwright active Revision Request identity is inconsistent")
        return message

    def _revision_execution_identities(
        self,
        state: PlaywrightWorkflowState,
        request: RevisionRequestV1,
    ) -> dict[str, str]:
        rerun_from = min(
            AGENT_ORDER.index(item) for item in request.target_agent_ids
        )
        stage_names = {
            NARRATIVE_ARCHITECT_ID: "narrative",
            SCRIPTWRITER_ID: "script",
            EVIDENCE_CITATION_EDITOR_ID: "citation",
            VISUAL_DIRECTOR_ID: "visual",
        }
        return {
            agent_id: (
                f"playwright_{stage_names[agent_id]}_upstream_"
                f"{state.upstream_revision_count}_revision_{request.revision_epoch}"
            )
            for agent_id in AGENT_ORDER[rerun_from:]
        }

    def _finalize_active_revision(
        self,
        state: PlaywrightWorkflowState,
        *,
        completed: bool,
        reason: str,
    ) -> None:
        if state.revision_control.phase != RevisionControlPhase.EXECUTING.value:
            return
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        result_artifacts: list[RevisionArtifactRef] = []
        if completed and state.final_script_package is not None:
            package_id = str(
                state.final_script_package.get("final_script_package_id") or ""
            )
            if package_id:
                result_artifacts.append(
                    RevisionArtifactRef(
                        artifact_type="playwright.final_script_package",
                        artifact_id=package_id,
                        sha256=canonical_sha256(state.final_script_package),
                    )
                )
        elif state.deterministic_validation is not None:
            validation_id = str(
                state.deterministic_validation.get("validation_id") or ""
            )
            if validation_id:
                result_artifacts.append(
                    RevisionArtifactRef(
                        artifact_type="playwright.deterministic_validation",
                        artifact_id=validation_id,
                        sha256=canonical_sha256(state.deterministic_validation),
                    )
                )
        provider_tasks = self._revision_execution_identities(state, request)
        result_id = "revision_result_" + uuid5(
            NAMESPACE_URL, f"{request.revision_request_id}:result"
        ).hex
        result = RevisionResultV1.create(
            revision_result_id=result_id,
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=state.workflow_id,
            requester_layer=LayerId.PLAYWRIGHT,
            producer_layer=LayerId.PLAYWRIGHT,
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
                    result_artifact_ids=[item.artifact_id for item in result_artifacts],
                )
                for finding_id in request.source_finding_ids
            ],
            human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            provider_reservation_ids=list(provider_tasks.values()),
            retrieval_reservation_ids=[],
            provider_call_count=len(provider_tasks),
            retrieval_call_count=0,
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
                    objective="Return the audited Playwright internal Revision result",
                    payload=result.model_dump(mode="json"),
                    routing=PMPRouting(revision_target=None, reply_required=False),
                    metadata=PMPMetadata(
                        status=(
                            MessageStatus.COMPLETED
                            if completed
                            else MessageStatus.REVISION_REQUIRED
                        )
                    ),
                ).model_dump(mode="json"),
                "message_id": result_message_id,
            }
        )
        self.revision_exchange.create_internal_result_once(
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
                layer=LayerId.PLAYWRIGHT,
                event_type=RevisionAuditEventType.RESULT_WRITTEN,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[item.artifact_id for item in result_artifacts],
                reservation_ids=list(provider_tasks.values()),
                reason=reason,
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

    def _record_revision_audit(
        self,
        state: PlaywrightWorkflowState,
        event: RevisionAuditEvent,
    ) -> None:
        self.revision_exchange.create_audit_event_once(event)
        if event.audit_event_id not in state.revision_control.audit_event_ids:
            state.revision_control.audit_event_ids.append(event.audit_event_id)

    async def start(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        try:
            return self.repository.load(workflow_id)
        except FileNotFoundError:
            pass
        return await self.start_from_message(
            self.repository.load_conclusion_handoff(workflow_id),
            progress_callback=progress_callback,
        )

    async def start_from_message(
        self,
        handoff: PMPMessage,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        manager_snapshot = self.rd_loader.load(self.agent_id)
        runtime = RoleDefinitionExtractor().extract_runtime_config(manager_snapshot)
        if not self.demo_safe_mode and runtime.revision_limit is not None:
            self.max_revisions = runtime.revision_limit
        self._validate_envelope(handoff)
        payload = handoff.payload
        if not payload.get("final_conclusion"):
            raise ValueError("HANDOFF_REJECTED: Final Conclusion is required")
        final = dict(payload["final_conclusion"])
        package = dict(payload.get("conclusion_package") or {})
        selection = dict(payload.get("human_selection") or {})
        trace = dict(payload.get("traceability_manifest") or {})
        state = PlaywrightWorkflowState(
            workflow_id=handoff.workflow_id,
            status=PlaywrightStatus.VALIDATING_HANDOFF,
            conclusion_handoff=handoff.model_dump(mode="json"),
            final_conclusion=final,
            conclusion_package=package,
            human_selection=selection,
            traceability_manifest=trace,
            final_conclusion_hash=canonical_hash(final),
            message_history=[handoff],
            role_definition_usage=[manager_snapshot.trace()],
            limitations=list(payload.get("limitations_to_disclose") or final.get("limitations") or []),
        )
        if not selection:
            state.status = PlaywrightStatus.BLOCKED
            state.error = {"stage": "handoff", "code": "HUMAN_SELECTION_MISSING", "message": "Human Selection is required before script production"}
            self.repository.save(state)
            return state
        problems = self._handoff_problems(payload)
        self.repository.save(state)
        if problems:
            return await self._request_upstream_revision(state, problems, progress_callback)
        context = self._build_production_context(state, payload)
        state.production_context = context.model_dump(mode="json")
        self.repository.save(state)
        await self._emit(progress_callback, f"Playwright Workflow開始: {state.workflow_id}")
        return await self._run(state, rerun_from=0, progress_callback=progress_callback)

    async def resume(
        self,
        workflow_id: str,
        *,
        actor_id: str = "cli.operator",
        actor_source: str = "CLI",
        reason: str = "Operator authorized Playwright resume after Conclusion Revision",
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        state = self.repository.load(workflow_id)
        if state.status != PlaywrightStatus.WAITING_UPSTREAM_REVISION.value:
            raise ValueError("Playwright workflow is not waiting for an upstream revision")
        canonical_request_message: PMPMessage | None = None
        canonical_result_message: PMPMessage | None = None
        if (
            state.revision_control.phase
            == RevisionControlPhase.WAITING_UPSTREAM_RESULT.value
        ):
            canonical_request_message = self._active_upstream_request_message(state)
            request = RevisionRequestV1.model_validate(
                canonical_request_message.payload
            )
            self.revision_exchange.validator.validate_current_base_artifacts(
                request,
                {
                    (
                        "conclusion.final_conclusion",
                        str(state.final_conclusion.get("final_conclusion_id") or ""),
                    ): canonical_sha256(state.final_conclusion),
                    (
                        "conclusion.conclusion_package",
                        str(state.conclusion_package.get("conclusion_package_id") or ""),
                    ): canonical_sha256(state.conclusion_package),
                    (
                        "conclusion.human_selection",
                        str(state.human_selection.get("selection_id") or ""),
                    ): canonical_sha256(state.human_selection),
                },
            )
            try:
                canonical_result_message = self.revision_exchange.load_result(
                    requester_layer=LayerId.PLAYWRIGHT,
                    workflow_id=state.workflow_id,
                    revision_request_id=request.revision_request_id,
                    request_message=canonical_request_message,
                )
            except FileNotFoundError:
                canonical_result_message = None
        handoff = self.repository.load_conclusion_handoff(workflow_id)
        if handoff.message_id == state.conclusion_handoff.get("message_id"):
            raise ValueError("Conclusionから新しいrevision resultがまだ届いていません")
        self._validate_envelope(handoff)
        payload = handoff.payload
        if not payload.get("final_conclusion") or not payload.get("human_selection"):
            raise ValueError("Revised Conclusion handoff is incomplete")
        if canonical_request_message is not None:
            if canonical_result_message is None:
                canonical_result_message = self._adapt_legacy_conclusion_revision_result(
                    state,
                    request_message=canonical_request_message,
                    handoff=handoff,
                )
            self._consume_conclusion_revision_result(
                state,
                request_message=canonical_request_message,
                result_message=canonical_result_message,
                handoff=handoff,
                actor_id=actor_id,
                actor_source=actor_source,
                reason=reason,
            )
        state.conclusion_handoff = handoff.model_dump(mode="json")
        state.final_conclusion = dict(payload["final_conclusion"])
        state.conclusion_package = dict(payload["conclusion_package"])
        state.human_selection = dict(payload["human_selection"])
        state.traceability_manifest = dict(payload["traceability_manifest"])
        state.final_conclusion_hash = canonical_hash(state.final_conclusion)
        state.production_context = None
        state.narrative_blueprint = None
        state.script_draft = None
        state.citation_validated_script = None
        state.citation_manifest = None
        state.visual_plan = None
        state.final_script_package = None
        state.delivery_paths = {}
        state.delivered = False
        state.deterministic_validation = None
        state.final_gate_result = None
        state.completed_agents = []
        state.failed_agents = []
        state.current_agent_ids = []
        state.message_history.append(handoff)
        state.limitations = list(payload.get("limitations_to_disclose") or state.final_conclusion.get("limitations") or [])
        state.error = None
        problems = self._handoff_problems(payload)
        if problems:
            self.repository.save(state)
            return await self._request_upstream_revision(state, problems, progress_callback)
        state.production_context = self._build_production_context(state, payload).model_dump(mode="json")
        self.repository.save(state)
        await self._emit(progress_callback, "Conclusion修正結果を受領し、Playwrightを再開します")
        return await self._run(state, rerun_from=0, progress_callback=progress_callback)

    def _active_upstream_request_message(
        self,
        state: PlaywrightWorkflowState,
    ) -> PMPMessage:
        request_id = state.revision_control.active_request_id
        message_id = state.revision_control.active_request_message_id
        if not request_id or not message_id:
            raise ValueError("Playwright has no active upstream Revision Request")
        message = next(
            (item for item in state.message_history if item.message_id == message_id),
            None,
        )
        if message is None:
            message = self.revision_exchange.load_request(
                target_layer=LayerId.CONCLUSION,
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
            )
        request = self.revision_exchange.validator.validate_request_message(message)
        if (
            request.revision_request_id != request_id
            or request.source_layer != LayerId.PLAYWRIGHT.value
            or request.target_layer != LayerId.CONCLUSION.value
        ):
            raise ValueError("Playwright upstream Revision identity is inconsistent")
        return message

    def _adapt_legacy_conclusion_revision_result(
        self,
        state: PlaywrightWorkflowState,
        *,
        request_message: PMPMessage,
        handoff: PMPMessage,
    ) -> PMPMessage:
        request = RevisionRequestV1.model_validate(request_message.payload)
        final = dict(handoff.payload["final_conclusion"])
        final_id = str(final.get("final_conclusion_id") or "")
        if not final_id:
            raise ValueError("Legacy Conclusion handoff has no final identity")
        result = RevisionResultV1.create(
            revision_result_id=(
                "revision_result_"
                + uuid5(
                    NAMESPACE_URL,
                    f"{request.revision_request_id}:legacy-result",
                ).hex
            ),
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=state.workflow_id,
            requester_layer=LayerId.PLAYWRIGHT,
            producer_layer=LayerId.CONCLUSION,
            revision_epoch=request.revision_epoch,
            status=RevisionExecutionStatus.COMPLETED,
            base_artifacts=request.base_artifacts,
            result_artifacts=[
                RevisionArtifactRef(
                    artifact_type="conclusion.final_conclusion",
                    artifact_id=final_id,
                    sha256=canonical_sha256(final),
                )
            ],
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id=finding_id,
                    outcome=RevisionFindingOutcome.RESOLVED,
                    reason="Validated legacy Conclusion handoff adapted without Provider calls",
                    result_artifact_ids=[final_id],
                )
                for finding_id in request.source_finding_ids
            ],
            human_selection_impact=request.expected_human_selection_impact,
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
                    objective="Adapt the validated legacy Conclusion revision handoff",
                    payload=result.model_dump(mode="json"),
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

    def _consume_conclusion_revision_result(
        self,
        state: PlaywrightWorkflowState,
        *,
        request_message: PMPMessage,
        result_message: PMPMessage,
        handoff: PMPMessage,
        actor_id: str,
        actor_source: str,
        reason: str,
    ) -> None:
        request = RevisionRequestV1.model_validate(request_message.payload)
        result = RevisionResultV1.model_validate(result_message.payload)
        final = dict(handoff.payload["final_conclusion"])
        artifact = next(
            (
                item
                for item in result.result_artifacts
                if item.artifact_type == "conclusion.final_conclusion"
            ),
            None,
        )
        if (
            result.status != RevisionExecutionStatus.COMPLETED.value
            or artifact is None
            or artifact.artifact_id != str(final.get("final_conclusion_id") or "")
            or artifact.sha256 != canonical_sha256(final)
        ):
            raise ValueError("Conclusion Revision Result does not match its handoff")
        old_selection_id = str(state.human_selection.get("selection_id") or "")
        new_selection = dict(handoff.payload["human_selection"])
        new_selection_id = str(new_selection.get("selection_id") or "")
        if (
            result.human_selection_impact
            == HumanSelectionImpact.UNCHANGED.value
            and new_selection != state.human_selection
        ):
            raise ValueError("UNCHANGED Conclusion Revision altered Human Selection")
        if (
            result.human_selection_impact
            == HumanSelectionImpact.RESELECTION_REQUIRED.value
            and new_selection_id == old_selection_id
        ):
            raise ValueError("Conclusion Revision required a new Human Selection")
        provider_tasks = {
            agent_id: self._logical_task_id(state, agent_id)
            for agent_id in AGENT_ORDER
        }
        authorization = RevisionExecutionAuthorization(
            authorization_id=(
                "revision_authorization_"
                + uuid5(
                    NAMESPACE_URL,
                    f"{request.revision_request_id}:playwright-resume",
                ).hex
            ),
            workflow_id=state.workflow_id,
            revision_request_id=request.revision_request_id,
            executing_layer=LayerId.PLAYWRIGHT,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            max_provider_calls=len(provider_tasks),
            max_retrieval_calls=0,
        )
        try:
            current = self.revision_exchange.load_authorization(
                executing_layer=LayerId.PLAYWRIGHT,
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
            )
            if (
                current.actor_id != actor_id
                or current.actor_source != actor_source
                or current.reason != reason
            ):
                raise ValueError("Playwright resume was authorized differently")
            authorization = current
        except FileNotFoundError:
            self.revision_exchange.create_authorization_once(authorization)
        consumed = self.revision_exchange.consume_authorization(
            authorization,
            provider_reservation_ids=list(provider_tasks.values()),
            retrieval_reservation_ids=[],
        )
        for message in (result_message,):
            if not any(item.message_id == message.message_id for item in state.message_history):
                state.message_history.append(message)
        for event in (
            RevisionAuditEvent(
                audit_event_id=f"result_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PLAYWRIGHT,
                event_type=RevisionAuditEventType.RESULT_CONSUMED,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[artifact.artifact_id],
                reason="Validated correlated Conclusion Revision Result",
            ),
            RevisionAuditEvent(
                audit_event_id=f"resume_authorization_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PLAYWRIGHT,
                event_type=RevisionAuditEventType.AUTHORIZATION_CONSUMED,
                actor_id=actor_id,
                reservation_ids=list(provider_tasks.values()),
                reason=reason,
                created_at=consumed.consumed_at,
            ),
        ):
            self._record_revision_audit(state, event)
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": RevisionControlPhase.COMPLETED.value,
                "active_result_id": result.revision_result_id,
                "pending_request_ids": [],
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
                        [
                            *state.revision_control.consumed_result_ids,
                            result.revision_result_id,
                        ]
                    )
                ),
            }
        )

    async def recover(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> PlaywrightWorkflowState:
        """Resume from the first missing valid Playwright checkpoint."""

        state = self.repository.load(workflow_id)
        if state.status == PlaywrightStatus.COMPLETED.value:
            return state
        if state.status == PlaywrightStatus.BLOCKED.value:
            return await self._recover_deterministic_artifact(
                state,
                progress_callback=progress_callback,
            )
        if (
            state.status == PlaywrightStatus.VALIDATING_PACKAGE.value
            and state.deterministic_repair_count > 0
        ):
            self._validate_recovery_identity(state)
            return await self._run(
                state,
                rerun_from=len(AGENT_ORDER),
                progress_callback=progress_callback,
            )
        if state.status != PlaywrightStatus.FAILED.value:
            raise ValueError("Playwright recovery requires a FAILED workflow")
        self._validate_recovery_identity(state)
        promoted_bindings = self._promote_saved_capability_repairs(state)
        restored_agents = self._restore_saved_stage_responses(state)
        rerun_from = self._first_missing_stage(state)
        next_agent = AGENT_ORDER[rerun_from] if rerun_from < len(AGENT_ORDER) else None
        task_id_overrides: dict[str, str] = {}
        model_overrides: dict[str, str] = {}

        try:
            request, error_response = self._latest_failed_provider_exchange(state)
        except ValueError as exc:
            if str(exc) != "No failed Playwright Provider exchange was found":
                raise
            request = None
            error_response = None
        if request is not None and next_agent is not None:
            if request.receiver_agent_id != next_agent:
                if request.receiver_agent_id in restored_agents:
                    request = None
                else:
                    raise ValueError(
                        "Saved Playwright checkpoints do not match the failed Provider stage"
                    )
        if request is not None and next_agent is not None:
            original_task_id = self._request_task_id(request)
            if original_task_id.endswith(PROVIDER_CAPABILITY_REPAIR_SUFFIX):
                raise ValueError(
                    "The one-time Playwright provider capability repair is exhausted"
                )
            if original_task_id.endswith(OPERATOR_RETRY_SUFFIX):
                raise ValueError("The one-time Playwright operator retry is exhausted")
            agent = self.registry.get(next_agent)
            provider_id = getattr(agent.provider, "provider_id", None)
            if not isinstance(provider_id, str):
                raise ValueError("Playwright Provider has no stable logical provider ID")
            capability_authorization = (
                self.provider_capability_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=original_task_id,
                )
            )
            if capability_authorization is not None:
                if (
                    capability_authorization.status != "PENDING"
                    or error_response is None
                    or capability_authorization.source_error_message_id
                    != error_response.message_id
                ):
                    raise ValueError(
                        "Playwright provider capability repair authorization does not match "
                        "the saved failed exchange"
                    )
                if self._has_unanswered_task_request(
                    state,
                    next_agent,
                    capability_authorization.repair_task_id,
                ):
                    raise ValueError(
                        "Playwright recovery found an unanswered provider capability "
                        "repair request; automatic redispatch is blocked"
                    )
                task_id_overrides[next_agent] = (
                    capability_authorization.repair_task_id
                )
                model_overrides[next_agent] = (
                    capability_authorization.repair_model_id
                )
            else:
                authorization = self.provider_retry_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=original_task_id,
                )
                if (
                    authorization is None
                    or authorization.status != "PENDING"
                    or error_response is None
                    or authorization.source_error_message_id
                    != error_response.message_id
                ):
                    raise ValueError(
                        "Playwright recovery found a Provider call without a reusable response; "
                        "explicit provider retry or capability repair authorization is required"
                    )
                if self._has_unanswered_task_request(
                    state,
                    next_agent,
                    authorization.retry_task_id,
                ):
                    raise ValueError(
                        "Playwright recovery found an unanswered provider retry request; "
                        "automatic redispatch is blocked"
                    )
                task_id_overrides[next_agent] = authorization.retry_task_id
        elif next_agent is not None and self._has_unanswered_stage_request(
            state,
            next_agent,
        ):
            raise ValueError(
                "Playwright recovery found an unanswered Provider request; "
                "an explicit audited retry path is required"
            )
        elif next_agent is not None and self._has_stage_request(state, next_agent):
            raise ValueError(
                "Playwright recovery found a saved Provider exchange without a reusable "
                "response; automatic redispatch is blocked"
            )

        if next_agent is not None:
            self._clear_from(state, rerun_from)

        state.error = None
        state.current_agent_ids = []
        state.failed_agents = [
            agent_id
            for agent_id in state.failed_agents
            if agent_id != next_agent and agent_id not in restored_agents
        ]
        self.repository.save(state)
        await self._emit(
            progress_callback,
            "Playwright checkpoint recovery: reusing completed stages; next stage "
            + (next_agent or "deterministic validation and delivery"),
        )
        if promoted_bindings:
            await self._emit(
                progress_callback,
                "Restored verified Provider model compatibility: "
                + ", ".join(promoted_bindings),
            )
        return await self._run(
            state,
            rerun_from=rerun_from,
            task_id_overrides=task_id_overrides,
            model_overrides=model_overrides,
            progress_callback=progress_callback,
        )

    async def _recover_deterministic_artifact(
        self,
        state: PlaywrightWorkflowState,
        *,
        progress_callback: ProgressCallback | None,
    ) -> PlaywrightWorkflowState:
        """Repair an allowlisted local artifact without consuming LLM revision budget."""

        if state.deterministic_validation is None or state.final_gate_result is None:
            raise ValueError(
                "BLOCKED Playwright workflow has no deterministic repairable Final Gate"
            )
        self._validate_recovery_identity(state)
        result = self.deterministic_repairer.repair(state)
        state.citation_manifest = result.citation_manifest.model_dump(mode="json")
        state.citation_validated_script = (
            result.citation_validated_script.model_dump(mode="json")
        )
        state.deterministic_repair_count += 1
        state.deterministic_repair_history.append(result.record)
        state.status = PlaywrightStatus.VALIDATING_PACKAGE
        state.error = None
        state.current_agent_ids = []

        # The repair artifact and Citation Manifest are independently durable.
        # Repeating this deterministic write after a process fault is safe because
        # both identities are content-derived and the workflow checkpoint is saved
        # before Final Gate re-evaluation or Delivery.
        self.repository.save_deterministic_repair(
            result.record,
            state.workflow_id,
        )
        self.repository.save_citation_manifest(
            result.citation_manifest,
            state.workflow_id,
        )
        self.repository.save(state)
        await self._emit(
            progress_callback,
            "Playwright deterministic citation repair completed without Provider calls: "
            + result.record.repair_id,
        )
        return await self._run(
            state,
            rerun_from=len(AGENT_ORDER),
            progress_callback=progress_callback,
        )

    async def _run(
        self,
        state: PlaywrightWorkflowState,
        *,
        rerun_from: int,
        task_id_overrides: dict[str, str] | None = None,
        model_overrides: dict[str, str] | None = None,
        progress_callback: ProgressCallback | None,
    ) -> PlaywrightWorkflowState:
        task_id_overrides = task_id_overrides or {}
        model_overrides = model_overrides or {}
        while True:
            try:
                context = ProductionContext.model_validate(state.production_context)
                revision_context = self._latest_revision_context(state)
                if rerun_from <= 0 or not state.narrative_blueprint:
                    state.status = PlaywrightStatus.DESIGNING_NARRATIVE
                    narrative = await self._execute_agent(
                        state,
                        NARRATIVE_ARCHITECT_ID,
                        NarrativeDesignTask(
                            task_id=task_id_overrides.get(NARRATIVE_ARCHITECT_ID)
                            or self._logical_task_id(state, NARRATIVE_ARCHITECT_ID),
                            production_context=context,
                            revision_context=revision_context,
                        ),
                        NarrativeBlueprint,
                        model_override=model_overrides.get(NARRATIVE_ARCHITECT_ID),
                    )
                    state.narrative_blueprint = narrative.model_dump(mode="json")
                    self.repository.save_narrative(narrative, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Narrative Architect完了")
                else:
                    narrative = NarrativeBlueprint.model_validate(state.narrative_blueprint)

                if rerun_from <= 1 or not state.script_draft:
                    state.status = PlaywrightStatus.WRITING_SCRIPT
                    script = await self._execute_agent(
                        state,
                        SCRIPTWRITER_ID,
                        ScriptWritingTask(
                            task_id=task_id_overrides.get(SCRIPTWRITER_ID)
                            or self._logical_task_id(state, SCRIPTWRITER_ID),
                            production_context=context,
                            narrative_blueprint=narrative,
                            revision_context=revision_context,
                        ),
                        ScriptDraft,
                        model_override=model_overrides.get(SCRIPTWRITER_ID),
                    )
                    state.script_draft = script.model_dump(mode="json")
                    self.repository.save_script(script, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Scriptwriter完了")
                else:
                    script = ScriptDraft.model_validate(state.script_draft)

                if rerun_from <= 2 or not state.citation_manifest or not state.citation_validated_script:
                    state.status = PlaywrightStatus.EDITING_CITATIONS
                    citation_result = await self._execute_agent(
                        state,
                        EVIDENCE_CITATION_EDITOR_ID,
                        CitationEditingTask(
                            task_id=task_id_overrides.get(EVIDENCE_CITATION_EDITOR_ID)
                            or self._logical_task_id(state, EVIDENCE_CITATION_EDITOR_ID),
                            production_context=context,
                            script_draft=script,
                            revision_context=revision_context,
                        ),
                        CitationEditingResult,
                        model_override=model_overrides.get(
                            EVIDENCE_CITATION_EDITOR_ID
                        ),
                    )
                    validated_script, citation_manifest = (
                        self._preserve_canonical_disclosures(
                            citation_result,
                            context,
                            script,
                        )
                    )
                    state.citation_validated_script = validated_script.model_dump(mode="json")
                    state.citation_manifest = citation_manifest.model_dump(mode="json")
                    self.repository.save_citation_manifest(citation_manifest, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Evidence & Citation Editor完了")
                else:
                    validated_script = CitationValidatedScript.model_validate(state.citation_validated_script)
                    citation_manifest = CitationManifest.model_validate(state.citation_manifest)

                # Script Draft owns the claim set.  Bind and persist that
                # canonical set before any downstream Provider receives the
                # Citation Manifest, including recovery from older checkpoints.
                citation_manifest = self._bind_canonical_manifest_claims(
                    citation_manifest,
                    script,
                )
                canonical_manifest_payload = citation_manifest.model_dump(mode="json")
                if state.citation_manifest != canonical_manifest_payload:
                    state.citation_manifest = canonical_manifest_payload
                    self.repository.save_citation_manifest(
                        citation_manifest,
                        state.workflow_id,
                    )
                    self.repository.save(state)
                self.validator.assert_manifest_claim_contract(
                    script_draft=script,
                    citation_manifest=citation_manifest,
                )

                if rerun_from <= 3 or not state.visual_plan:
                    state.status = PlaywrightStatus.DESIGNING_VISUALS
                    visual_plan = await self._execute_agent(
                        state,
                        VISUAL_DIRECTOR_ID,
                        VisualDirectionTask(
                            task_id=task_id_overrides.get(VISUAL_DIRECTOR_ID)
                            or self._logical_task_id(state, VISUAL_DIRECTOR_ID),
                            production_context=context,
                            citation_validated_script=validated_script,
                            citation_manifest=citation_manifest,
                            revision_context=revision_context,
                        ),
                        VisualPlan,
                        model_override=model_overrides.get(VISUAL_DIRECTOR_ID),
                    )
                    state.visual_plan = visual_plan.model_dump(mode="json")
                    self.repository.save_visual_plan(visual_plan, state.workflow_id)
                    self.repository.save(state)
                    await self._emit(progress_callback, "Visual Director完了")
                else:
                    visual_plan = VisualPlan.model_validate(state.visual_plan)

                state.status = PlaywrightStatus.VALIDATING_PACKAGE
                validation = self.validator.validate(
                    production_context=context,
                    narrative=narrative,
                    script_draft=script,
                    validated_script=validated_script,
                    citation_manifest=citation_manifest,
                    visual_plan=visual_plan,
                    final_conclusion=state.final_conclusion,
                    expected_final_conclusion_hash=state.final_conclusion_hash,
                )
                state.deterministic_validation = validation.model_dump(mode="json")
                gate = self._final_gate(state, validation)
                state.final_gate_result = gate.model_dump(mode="json")
                self.repository.save(state)
            except Exception as exc:
                return await self._fail(state, f"Playwright生成に失敗しました: {exc}", progress_callback)

            active_revision_finished = (
                state.revision_control.phase
                == RevisionControlPhase.EXECUTING.value
            )
            if gate.status == PlaywrightGateStatus.UPSTREAM_REVISION_REQUIRED.value:
                self._finalize_active_revision(
                    state,
                    completed=False,
                    reason=gate.delivery_readiness,
                )
                return await self._request_upstream_revision(
                    state,
                    gate.upstream_revision_requests,
                    progress_callback,
                )
            if gate.status == PlaywrightGateStatus.BLOCKED.value:
                self._finalize_active_revision(
                    state,
                    completed=False,
                    reason=gate.delivery_readiness,
                )
                state.status = PlaywrightStatus.BLOCKED
                state.current_agent_ids = []
                state.error = {"stage": "final_gate", "message": gate.delivery_readiness}
                self.repository.save(state)
                return state
            if gate.status == PlaywrightGateStatus.REVISION_REQUIRED.value:
                self._finalize_active_revision(
                    state,
                    completed=False,
                    reason=gate.delivery_readiness,
                )
                if self.demo_safe_mode:
                    if not active_revision_finished:
                        self._plan_internal_revision(state, gate)
                    state.status = PlaywrightStatus.BLOCKED
                    state.current_agent_ids = []
                    state.error = {
                        "stage": "final_gate",
                        "message": (
                            "Demo Safe Mode stopped automatic Playwright revision; "
                            "use --playwright-revise for one audited cycle"
                        ),
                    }
                    self.repository.save(state)
                    return state
                self._plan_internal_revision(state, gate)
                self.repository.save(state)
                return await self._execute_active_revision(
                    state,
                    actor_id=self.agent_id,
                    actor_source="SYSTEM",
                    reason="Safe Mode is disabled; execute saved Playwright revision",
                    progress_callback=progress_callback,
                )

            package = self._build_final_package(
                state,
                context,
                script,
                validated_script,
                citation_manifest,
                visual_plan,
                gate,
            )
            state.final_script_package = package.model_dump(mode="json")
            self._finalize_active_revision(
                state,
                completed=True,
                reason=gate.delivery_readiness,
            )
            self.repository.save_final_package(package)
            state.delivery_paths = self.repository.save_deliveries(package)
            delivery = PMPMessage.create(
                workflow_id=state.workflow_id,
                parent_message_id=state.message_history[-1].message_id,
                sender_agent_id=self.agent_id,
                receiver_agent_id="system.final_output",
                message_type=MessageType.FINAL_SCRIPT_DELIVERY,
                objective="Deliver the completed Final Script Package",
                payload={
                    "final_script_package_id": package.final_script_package_id,
                    "production_summary": package.production_summary,
                    "delivery_paths": state.delivery_paths,
                },
                constraints={"final_conclusion_changes_allowed": False},
                context=PMPContext(current_stage="playwright.completed", previous_stage="playwright.final_gate", next_stage="delivery"),
                metadata=PMPMetadata(
                    status=MessageStatus.COMPLETED,
                    extensions={"role_definition": state.role_definition_usage[0]},
                ),
            )
            self.pmp_validator.validate(delivery)
            if not any(
                item.message_type == MessageType.FINAL_SCRIPT_DELIVERY.value
                and item.payload.get("final_script_package_id")
                == package.final_script_package_id
                for item in state.message_history
            ):
                state.message_history.append(delivery)
            state.delivered = True
            state.status = PlaywrightStatus.COMPLETED
            state.current_agent_ids = []
            state.error = None
            state.completed_at = utc_now()
            self.repository.save(state)
            await self._emit(progress_callback, "Final Script PackageをJSON・Markdownで納品しました")
            return state

    @staticmethod
    def _preserve_canonical_disclosures(
        citation_result: CitationEditingResult,
        context: ProductionContext,
        script: ScriptDraft,
    ) -> tuple[CitationValidatedScript, CitationManifest]:
        """Keep canonical disclosure metadata under Manager ownership.

        The Provider edits prose and citations.  It must not be responsible for
        byte-for-byte retranscription of a potentially long upstream list that
        the Final Script Package already carries as canonical production notes.
        """

        limitations = list(dict.fromkeys(context.limitations_to_disclose))
        validated_script = citation_result.citation_validated_script.model_copy(
            update={"limitations": limitations}
        )
        manifest = citation_result.citation_manifest.model_copy(
            update={
                "supported_claim_ids": canonical_script_claim_ids(script),
                "disclosure_checks": [
                    DisclosureCheck(limitation=item, preserved=True)
                    for item in limitations
                ]
            }
        )
        return validated_script, manifest

    @staticmethod
    def _bind_canonical_manifest_claims(
        manifest: CitationManifest,
        script: ScriptDraft,
    ) -> CitationManifest:
        return manifest.model_copy(
            update={"supported_claim_ids": canonical_script_claim_ids(script)}
        )

    async def _execute_agent(
        self,
        state,
        agent_id: str,
        task,
        output_schema: type[BaseModel],
        *,
        model_override: str | None = None,
    ):
        message_type = (
            MessageType.REVISION_REQUEST
            if getattr(task, "revision_context", None)
            else MessageType.TASK
        )
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=state.message_history[-1].message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=agent_id,
            message_type=message_type,
            objective=f"Execute {agent_id} for the approved Final Conclusion",
            payload=task.model_dump(mode="json"),
            constraints={
                "preserve_final_conclusion": True,
                "new_evidence_allowed": False,
                "requested_action": self._task_action(agent_id),
            },
            context=PMPContext(current_stage="playwright.manager", previous_stage=state.status, next_stage=agent_id),
            metadata=PMPMetadata(status=MessageStatus.QUEUED),
        )
        self.pmp_validator.validate(request)
        state.message_history.append(request)
        state.current_agent_ids = [agent_id]
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
        if response.workflow_id != request.workflow_id or response.parent_message_id != request.message_id:
            raise ValueError(f"Invalid PMP response correlation from {agent_id}")
        if response.sender_agent_id != agent_id or response.receiver_agent_id != self.agent_id:
            raise ValueError(f"Invalid PMP response routing from {agent_id}")
        if response.message_type == MessageType.ERROR.value:
            if agent_id not in state.failed_agents:
                state.failed_agents.append(agent_id)
            raise RuntimeError(str(response.payload.get("message") or "Agent returned an error"))
        if response.message_type != MessageType.RESULT.value:
            raise ValueError(f"Unexpected PMP message type from {agent_id}: {response.message_type}")
        trace = response.metadata.extensions.get("role_definition")
        if trace and trace not in state.role_definition_usage:
            state.role_definition_usage.append(trace)
        if agent_id not in state.completed_agents:
            state.completed_agents.append(agent_id)
        if agent_id in state.failed_agents:
            state.failed_agents.remove(agent_id)
        self.repository.save(state)
        result = output_schema.model_validate(response.payload)
        task_id = task.task_id
        if task_id.endswith(PROVIDER_CAPABILITY_REPAIR_SUFFIX):
            self._record_verified_capability_repair(
                state,
                agent_id=agent_id,
                output_schema=output_schema,
                result_task_id=task_id,
                result_message_id=response.message_id,
            )
        return result

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

    def _record_verified_capability_repair(
        self,
        state: PlaywrightWorkflowState,
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
        original_task_id = result_task_id[
            : -len(PROVIDER_CAPABILITY_REPAIR_SUFFIX)
        ]
        authorization = self.provider_capability_repair_store.for_original_task(
            workflow_id=state.workflow_id,
            provider_id=provider_id,
            original_task_id=original_task_id,
        )
        if authorization is None:
            raise ValueError("Validated capability repair has no saved authorization")
        binding = self.provider_model_compatibility_store.record_verified_repair(
            authorization,
            output_schema_id=self._output_schema_id(output_schema),
            result_task_id=result_task_id,
            result_message_id=result_message_id,
        )
        return (
            f"{binding.agent_id}: {binding.incompatible_model_id} -> "
            f"{binding.compatible_model_id}"
        )

    def _promote_saved_capability_repairs(
        self,
        state: PlaywrightWorkflowState,
    ) -> list[str]:
        """Backfill bindings when the provider result preceded a write fault."""

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
                or not task_id.endswith(PROVIDER_CAPABILITY_REPAIR_SUFFIX)
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
                self._record_verified_capability_repair(
                    state,
                    agent_id=request.receiver_agent_id,
                    output_schema=agent.output_schema,
                    result_task_id=task_id,
                    result_message_id=response.message_id,
                )
            )
        return list(dict.fromkeys(promoted))

    def _build_production_context(self, state: PlaywrightWorkflowState, payload: dict[str, Any]) -> ProductionContext:
        final = state.final_conclusion
        package = state.conclusion_package
        selection = state.human_selection
        sources = state.traceability_manifest.get("sources") or payload.get("evidence_links") or []
        return ProductionContext(
            production_context_id=new_id("production_context"),
            workflow_id=state.workflow_id,
            final_conclusion_id=final["final_conclusion_id"],
            conclusion_package_id=package["conclusion_package_id"],
            human_selection_id=selection["selection_id"],
            topic=package.get("topic") or payload["topic"],
            central_question=package.get("decision_question") or payload["central_question"],
            selected_position=final["selected_position"],
            final_recommendation=final["final_recommendation"],
            target_audience=payload.get("target_audience") or self.target_audience,
            video_objective=payload.get("video_objective") or "一般的な意見を証拠と反論を含めて検証し、選択済み結論を説明する",
            desired_duration_seconds=int(payload.get("desired_duration_seconds") or self.target_duration_seconds),
            language=payload.get("language") or self.language,
            format=payload.get("format") or self.video_format,
            must_include_claim_ids=final["supporting_claim_ids"],
            must_include_evidence_ids=final["supporting_evidence_ids"],
            accepted_tradeoffs=final.get("accepted_tradeoffs", []),
            accepted_risks=final.get("accepted_risks", []),
            uncertainties=final.get("uncertainties", []),
            limitations_to_disclose=list(
                dict.fromkeys(state.limitations + final.get("limitations", []))
            ),
            human_evidence_decision=final.get("human_evidence_decision"),
            accepted_evidence_gaps=final.get("accepted_evidence_gaps", []),
            tone_constraints=payload.get("tone_constraints") or ["証拠強度に応じて断定の強さを調整する"],
            format_constraints=payload.get("format_constraints") or ["段落単位で引用とVisual Cueを追跡する"],
            source_manifest=sources,
        )

    def _final_gate(self, state: PlaywrightWorkflowState, validation: DeterministicValidationResult) -> PlaywrightFinalGateResult:
        findings = [item.model_dump(mode="json") for item in validation.findings]
        errors = [item for item in validation.findings if item.severity == ValidationSeverity.ERROR.value]
        upstream = [item for item in errors if item.upstream_required]
        if upstream:
            requests = [self._finding_to_upstream(state, item).model_dump(mode="json") for item in upstream]
            return PlaywrightFinalGateResult(
                final_gate_result_id=new_id("pw_gate"),
                status=PlaywrightGateStatus.UPSTREAM_REVISION_REQUIRED,
                findings=findings,
                blocking_finding_ids=[item.finding_id for item in upstream],
                upstream_revision_requests=requests,
                limitations_to_disclose=state.limitations,
                delivery_readiness="Conclusion revision required",
            )
        if errors:
            targets = list(dict.fromkeys(item.target_agent_id for item in errors if item.target_agent_id in AGENT_ORDER))
            if self.max_revisions <= state.revision_count or not targets:
                return PlaywrightFinalGateResult(
                    final_gate_result_id=new_id("pw_gate"),
                    status=PlaywrightGateStatus.BLOCKED,
                    findings=findings,
                    blocking_finding_ids=[item.finding_id for item in errors],
                    limitations_to_disclose=state.limitations,
                    delivery_readiness="Revision limit reached or no valid revision route remains",
                )
            return PlaywrightFinalGateResult(
                final_gate_result_id=new_id("pw_gate"),
                status=PlaywrightGateStatus.REVISION_REQUIRED,
                findings=findings,
                blocking_finding_ids=[item.finding_id for item in errors],
                revision_targets=targets,
                limitations_to_disclose=state.limitations,
                delivery_readiness="Targeted Playwright revision required",
            )
        status = PlaywrightGateStatus.APPROVED_WITH_LIMITATIONS if state.limitations else PlaywrightGateStatus.APPROVED
        return PlaywrightFinalGateResult(
            final_gate_result_id=new_id("pw_gate"),
            status=status,
            findings=findings,
            limitations_to_disclose=state.limitations,
            delivery_readiness="READY_WITH_LIMITATIONS" if state.limitations else "READY",
        )

    def _build_final_package(self, state, context, script, validated_script, manifest, visual, gate):
        paragraph_count = sum(len(section.paragraphs) for section in validated_script.sections)
        traceability = {
            **state.traceability_manifest,
            "final_conclusion_id": state.final_conclusion["final_conclusion_id"],
            "human_selection_id": state.human_selection["selection_id"],
            "paragraph_ids": [p.paragraph_id for s in validated_script.sections for p in s.paragraphs],
            "citation_mapping_ids": [item.citation_mapping_id for item in manifest.mappings],
            "visual_cue_ids": [item.visual_cue_id for item in visual.visual_cues],
        }
        return FinalScriptPackage(
            final_script_package_id=new_id("final_script_package"),
            workflow_id=state.workflow_id,
            final_conclusion_id=state.final_conclusion["final_conclusion_id"],
            human_selection_id=state.human_selection["selection_id"],
            title_candidates=script.title_candidates,
            thumbnail_text_candidates=script.thumbnail_text_candidates,
            script=validated_script,
            citation_manifest=manifest,
            visual_plan=visual,
            production_summary={
                "estimated_duration_seconds": script.estimated_duration_seconds,
                "estimated_character_count": script.estimated_character_count,
                "section_count": len(validated_script.sections),
                "paragraph_count": paragraph_count,
                "citation_count": len(manifest.mappings),
                "visual_cue_count": len(visual.visual_cues),
                "chart_request_count": len(visual.chart_requests),
            },
            limitations_to_disclose=state.limitations,
            unresolved_production_items=[
                item.model_dump(mode="json")
                for item in (
                    validated_script.unresolved_citation_issues
                    + visual.visual_integrity_warnings
                )
            ],
            traceability_manifest=traceability,
            final_gate_result=gate.model_dump(mode="json"),
        )

    async def _request_upstream_revision(self, state, problems, progress_callback):
        requests = []
        if problems and isinstance(problems[0], dict) and "revision_request_id" in problems[0]:
            requests = problems
        else:
            for item in problems:
                finding_id = new_id("pw_handoff_finding")
                requests.append(
                    UpstreamConclusionRevisionRequest(
                        revision_request_id=new_id("pw_upstream"),
                        final_conclusion_id=state.final_conclusion.get("final_conclusion_id") or state.conclusion_handoff.get("payload", {}).get("conclusion_id") or "unknown",
                        affected_claim_ids=list(state.final_conclusion.get("supporting_claim_ids") or []),
                        affected_evidence_ids=list(state.final_conclusion.get("supporting_evidence_ids") or []),
                        issue_type=item.get("code", "CONCLUSION_HANDOFF_INVALID") if isinstance(item, dict) else "CONCLUSION_HANDOFF_INVALID",
                        issue_description=item.get("message", str(item)) if isinstance(item, dict) else str(item),
                        required_resolution="Conclusionの正本とTraceabilityを修正し、新しいconclusion_handoffを発行する",
                        acceptance_conditions=["Human SelectionとFinal ConclusionのIDが一致する", "全Supporting IDがTraceability Manifestに存在する"],
                        source_finding_ids=[finding_id],
                    ).model_dump(mode="json")
                )
        source_finding_ids = list(
            dict.fromkeys(
                finding_id
                for item in requests
                for finding_id in item.get("source_finding_ids", [])
            )
        )
        if not source_finding_ids:
            raise ValueError("Playwright upstream revision has no correlated finding IDs")
        revision_epoch = max(
            state.upstream_revision_count,
            state.revision_control.revision_epoch,
        ) + 1
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.PLAYWRIGHT,
            target_layer=LayerId.CONCLUSION,
            revision_epoch=revision_epoch,
            source_review_id=state.message_history[-1].message_id,
            source_finding_ids=source_finding_ids,
        )
        impact = self._upstream_human_selection_impact(requests)
        request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.UPSTREAM,
            source_layer=LayerId.PLAYWRIGHT,
            target_layer=LayerId.CONCLUSION,
            revision_epoch=revision_epoch,
            root_revision_request_id=request_id,
            source_review_id=state.message_history[-1].message_id,
            source_finding_ids=source_finding_ids,
            target_agent_ids=(
                ["conclusion.manager"]
                if impact == HumanSelectionImpact.UNCHANGED
                else [
                    "conclusion.position_generator",
                    "conclusion.decision_evaluator",
                    "conclusion.decision_integrator",
                ]
            ),
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="conclusion.final_conclusion",
                    artifact_id=str(
                        state.final_conclusion.get("final_conclusion_id") or ""
                    ),
                    sha256=canonical_sha256(state.final_conclusion),
                ),
                RevisionArtifactRef(
                    artifact_type="conclusion.conclusion_package",
                    artifact_id=str(
                        state.conclusion_package.get("conclusion_package_id") or ""
                    ),
                    sha256=canonical_sha256(state.conclusion_package),
                ),
                RevisionArtifactRef(
                    artifact_type="conclusion.human_selection",
                    artifact_id=str(state.human_selection.get("selection_id") or ""),
                    sha256=canonical_sha256(state.human_selection),
                ),
            ],
            required_actions=list(
                dict.fromkeys(str(item["required_resolution"]) for item in requests)
            ),
            acceptance_conditions=list(
                dict.fromkeys(
                    str(condition)
                    for item in requests
                    for condition in item["acceptance_conditions"]
                )
            ),
            evidence_expansion_allowed=False,
            retrieval_allowed=False,
            expected_human_selection_impact=impact,
        )
        canonical_message_id = str(
            uuid5(NAMESPACE_URL, f"{request_id}:request-message")
        )
        canonical_message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=state.message_history[-1].message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id="conclusion.manager",
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Revise Conclusion artifacts required for valid script production",
                    payload=request.model_dump(mode="json"),
                    constraints={
                        "human_selection_impact": impact,
                        "new_evidence_collection_allowed": False,
                    },
                    context=PMPContext(
                        current_stage="playwright.upstream_revision",
                        previous_stage=state.status,
                        next_stage="conclusion.manager",
                    ),
                    routing=PMPRouting(
                        revision_target="conclusion.manager",
                        reply_required=True,
                    ),
                    metadata=PMPMetadata(
                        status=MessageStatus.REVISION_REQUIRED,
                        extensions={"role_definition": state.role_definition_usage[0]},
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
            parent_message_id=state.message_history[-1].message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="conclusion.manager",
            message_type=MessageType.REVISION_REQUEST,
            objective="Revise Conclusion artifacts required for script production",
            payload={
                "final_conclusion_id": state.final_conclusion.get("final_conclusion_id", "unknown"),
                "revision_requests": requests,
            },
            constraints={"preserve_human_selection": True, "new_evidence_collection_allowed": False},
            context=PMPContext(current_stage="playwright.upstream_revision", previous_stage=state.status, next_stage="conclusion"),
            routing=PMPRouting(revision_target="conclusion.manager", reply_required=True),
            metadata=PMPMetadata(status=MessageStatus.REVISION_REQUIRED, extensions={"role_definition": state.role_definition_usage[0]}),
        )
        self.pmp_validator.validate(message)
        self.repository.save_conclusion_revision_outbox(message)
        for saved_message in (canonical_message, message):
            if not any(
                item.message_id == saved_message.message_id
                for item in state.message_history
            ):
                state.message_history.append(saved_message)
        state.upstream_revision_count += 1
        state.upstream_revision_history.append(
            PlaywrightUpstreamRevisionRecord(
                iteration=state.upstream_revision_count,
                request_message_id=message.message_id,
                requests=requests,
            )
        )
        state.status = PlaywrightStatus.WAITING_UPSTREAM_REVISION
        state.revision_control = RevisionControlState(
            phase=RevisionControlPhase.WAITING_UPSTREAM_RESULT,
            revision_epoch=revision_epoch,
            active_request_id=request_id,
            active_request_message_id=canonical_message.message_id,
            root_revision_request_id=request_id,
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
                layer=LayerId.PLAYWRIGHT,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=canonical_message.message_id,
                artifact_ids=[item.artifact_id for item in request.base_artifacts],
                reason="Playwright Final Gate or handoff validation requires Conclusion repair",
            ),
        )
        state.current_agent_ids = []
        state.error = None
        self.repository.save(state)
        await self._emit(progress_callback, "Conclusionへ修正を要求し、Playwrightを待機状態にしました")
        return state

    @staticmethod
    def _upstream_human_selection_impact(
        requests: list[dict[str, Any]],
    ) -> HumanSelectionImpact:
        structural_codes = {
            "TRACE_CLAIM_IDS_MISSING",
            "TRACE_ANALYSIS_IDS_MISSING",
            "TRACE_EVIDENCE_IDS_MISSING",
            "TRACE_SOURCE_IDS_MISSING",
            "SOURCE_MANIFEST_MISSING",
            "SOURCE_MANIFEST_INVALID",
            "CONCLUSION_QUALITY_REVIEW_MISMATCH",
        }
        issue_types = {str(item.get("issue_type") or "") for item in requests}
        if issue_types and issue_types <= structural_codes:
            return HumanSelectionImpact.UNCHANGED
        return HumanSelectionImpact.RESELECTION_REQUIRED

    def _handoff_problems(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        final = payload.get("final_conclusion") or {}
        package = payload.get("conclusion_package") or {}
        selection = payload.get("human_selection") or {}
        trace = payload.get("traceability_manifest") or {}
        problems: list[dict[str, str]] = []

        def problem(code: str, message: str) -> None:
            problems.append({"code": code, "message": message})

        if final.get("workflow_id") != payload.get("workflow_metadata", {}).get("workflow_id", final.get("workflow_id")):
            problem("WORKFLOW_ID_MISMATCH", "Final Conclusion workflow_id does not match handoff metadata")
        if final.get("human_selection_id") != selection.get("selection_id"):
            problem("HUMAN_SELECTION_ID_MISMATCH", "Final Conclusion and Human Selection IDs do not match")
        if final.get("conclusion_package_id") != package.get("conclusion_package_id"):
            problem("CONCLUSION_PACKAGE_ID_MISMATCH", "Final Conclusion and Conclusion Package IDs do not match")
        if payload.get("human_evidence_decision") != final.get("human_evidence_decision"):
            problem(
                "HUMAN_EVIDENCE_DECISION_MISMATCH",
                "Human Evidence Decision copies do not match",
            )
        if payload.get("accepted_evidence_gaps") != final.get("accepted_evidence_gaps", []):
            problem(
                "ACCEPTED_EVIDENCE_GAPS_MISMATCH",
                "Accepted Evidence Gap copies do not match",
            )
        selected_ids = set(selection.get("selected_candidate_ids") or [])
        selected_position_id = final.get("selected_position", {}).get("position_candidate_id") or final.get("selected_position", {}).get("candidate_id")
        if selected_ids and selected_position_id not in selected_ids:
            problem("HUMAN_SELECTION_TARGET_MISMATCH", "Selected position is not included in Human Selection")
        package_review_payload = package.get("quality_review")
        top_level_review_payload = payload.get("quality_review")
        try:
            package_review = (
                ConclusionQualityReviewOutput.model_validate(package_review_payload)
                if package_review_payload is not None
                else None
            )
            top_level_review = (
                ConclusionQualityReviewOutput.model_validate(top_level_review_payload)
                if top_level_review_payload is not None
                else None
            )
        except Exception as exc:
            problem("CONCLUSION_QUALITY_REVIEW_INVALID", str(exc))
            review = None
        else:
            review = package_review or top_level_review
            if package_review is not None and top_level_review is not None:
                if package_review.model_dump(mode="json") != top_level_review.model_dump(
                    mode="json"
                ):
                    problem(
                        "CONCLUSION_QUALITY_REVIEW_MISMATCH",
                        "Conclusion Quality Review copies do not match",
                    )
        if review is None or review.status not in {"approved", "approved_with_conditions"}:
            problem("CONCLUSION_QUALITY_NOT_APPROVED", "Conclusion Package has not passed its Quality Gate")
        if review is None or review.playwright_readiness not in {"ready", "ready_with_conditions"}:
            problem("PLAYWRIGHT_NOT_READY", "Conclusion Package is not marked Playwright-ready")
        for trace_key, final_key in (
            ("claim_ids", "supporting_claim_ids"),
            ("analysis_ids", "supporting_analysis_ids"),
            ("evidence_ids", "supporting_evidence_ids"),
            ("source_ids", "supporting_source_ids"),
        ):
            missing = set(final.get(final_key) or []) - set(trace.get(trace_key) or [])
            if missing:
                problem(f"TRACE_{trace_key.upper()}_MISSING", f"Traceability Manifest is missing {sorted(missing)}")
        sources = trace.get("sources") or payload.get("evidence_links") or []
        if not sources:
            problem("SOURCE_MANIFEST_MISSING", "Traceability Manifest contains no sources")
        elif any(not item.get("evidence_id") or not item.get("source_id") for item in sources):
            problem("SOURCE_MANIFEST_INVALID", "Every source manifest item requires evidence_id and source_id")
        return problems

    def _validate_envelope(self, handoff: PMPMessage) -> None:
        validated = self.pmp_validator.validate(handoff)
        checks = [
            (validated.sender_agent_id == "conclusion.manager", "sender_agent_id must be conclusion.manager"),
            (validated.receiver_agent_id == self.agent_id, "receiver_agent_id must be playwright.manager"),
            (validated.message_type == MessageType.CONCLUSION_HANDOFF.value, "message_type must be conclusion_handoff"),
        ]
        for passed, message in checks:
            if not passed:
                raise ValueError(f"HANDOFF_REJECTED: {message}")

    @staticmethod
    def _finding_to_upstream(state, finding) -> UpstreamConclusionRevisionRequest:
        return UpstreamConclusionRevisionRequest(
            revision_request_id=new_id("pw_upstream"),
            final_conclusion_id=state.final_conclusion["final_conclusion_id"],
            affected_claim_ids=list(state.final_conclusion.get("supporting_claim_ids") or []),
            affected_evidence_ids=list(state.final_conclusion.get("supporting_evidence_ids") or []),
            issue_type=finding.code,
            issue_description=finding.message,
            required_resolution="Conclusion正本を修正して新しいHandoffを発行する",
            acceptance_conditions=["Final Conclusionの不変性とTraceabilityが検証できる"],
            source_finding_ids=[finding.finding_id],
        )

    @staticmethod
    def _task_action(agent_id: str) -> str:
        return {
            NARRATIVE_ARCHITECT_ID: "narrative_design",
            SCRIPTWRITER_ID: "script_drafting",
            EVIDENCE_CITATION_EDITOR_ID: "citation_validation",
            VISUAL_DIRECTOR_ID: "visual_direction",
        }[agent_id]

    @staticmethod
    def _clear_from(state: PlaywrightWorkflowState, index: int) -> None:
        fields = [
            "narrative_blueprint",
            "script_draft",
            "citation_validated_script",
            "citation_manifest",
            "visual_plan",
        ]
        first_field_by_agent_index = {0: 0, 1: 1, 2: 2, 3: 4}
        for field in fields[first_field_by_agent_index[index]:]:
            setattr(state, field, None)
        for agent_id in AGENT_ORDER[index:]:
            if agent_id in state.completed_agents:
                state.completed_agents.remove(agent_id)
        state.deterministic_validation = None
        state.final_gate_result = None
        state.final_script_package = None
        state.delivery_paths = {}
        state.delivered = False
        state.completed_at = None

    def _validate_recovery_identity(self, state: PlaywrightWorkflowState) -> None:
        handoff = PMPMessage.model_validate(state.conclusion_handoff)
        self._validate_envelope(handoff)
        if handoff.workflow_id != state.workflow_id:
            raise ValueError("Playwright recovery workflow identity mismatch")
        if canonical_hash(state.final_conclusion) != state.final_conclusion_hash:
            raise ValueError("Playwright recovery Final Conclusion hash mismatch")
        context = ProductionContext.model_validate(state.production_context)
        if (
            context.workflow_id != state.workflow_id
            or context.final_conclusion_id
            != state.final_conclusion.get("final_conclusion_id")
        ):
            raise ValueError("Playwright recovery Production Context identity mismatch")

    def _first_missing_stage(self, state: PlaywrightWorkflowState) -> int:
        try:
            context = ProductionContext.model_validate(state.production_context)
        except Exception:
            return 0
        try:
            narrative = NarrativeBlueprint.model_validate(state.narrative_blueprint)
            if narrative.production_context_id != context.production_context_id:
                return 0
        except Exception:
            return 0
        try:
            script = ScriptDraft.model_validate(state.script_draft)
            if script.narrative_blueprint_id != narrative.narrative_blueprint_id:
                return 1
        except Exception:
            return 1
        try:
            validated = CitationValidatedScript.model_validate(
                state.citation_validated_script
            )
            manifest = CitationManifest.model_validate(state.citation_manifest)
            if (
                validated.source_script_draft_id != script.script_draft_id
                or manifest.script_draft_id != script.script_draft_id
                or validated.citation_manifest_id != manifest.citation_manifest_id
            ):
                return 2
        except Exception:
            return 2
        try:
            visual = VisualPlan.model_validate(state.visual_plan)
            if (
                visual.citation_validated_script_id
                != validated.citation_validated_script_id
            ):
                return 3
        except Exception:
            return 3
        return 4

    def _restore_saved_stage_responses(
        self,
        state: PlaywrightWorkflowState,
    ) -> set[str]:
        """Promote validated billed responses after a checkpoint write fault."""

        restored: set[str] = set()
        for agent_id, output_schema in (
            (NARRATIVE_ARCHITECT_ID, NarrativeBlueprint),
            (SCRIPTWRITER_ID, ScriptDraft),
            (EVIDENCE_CITATION_EDITOR_ID, CitationEditingResult),
            (VISUAL_DIRECTOR_ID, VisualPlan),
        ):
            if self._stage_is_valid(state, agent_id):
                continue
            exchange = None
            for task_id in reversed(self._recoverable_stage_task_ids(state, agent_id)):
                exchange = self._saved_stage_result_exchange(
                    state,
                    agent_id=agent_id,
                    output_schema=output_schema,
                    task_id=task_id,
                )
                if exchange is not None:
                    break
            if exchange is None:
                break
            result, _response = exchange
            if not self._stage_result_matches_state(state, agent_id, result):
                break
            if agent_id == NARRATIVE_ARCHITECT_ID:
                state.narrative_blueprint = result.model_dump(mode="json")
            elif agent_id == SCRIPTWRITER_ID:
                state.script_draft = result.model_dump(mode="json")
            elif agent_id == EVIDENCE_CITATION_EDITOR_ID:
                state.citation_validated_script = (
                    result.citation_validated_script.model_dump(mode="json")
                )
                state.citation_manifest = result.citation_manifest.model_dump(
                    mode="json"
                )
            else:
                state.visual_plan = result.model_dump(mode="json")
            if agent_id not in state.completed_agents:
                state.completed_agents.append(agent_id)
            restored.add(agent_id)
        return restored

    def _stage_is_valid(self, state: PlaywrightWorkflowState, agent_id: str) -> bool:
        return self._first_missing_stage(state) > AGENT_ORDER.index(agent_id)

    def _saved_stage_result_exchange(
        self,
        state: PlaywrightWorkflowState,
        *,
        agent_id: str,
        output_schema: type[BaseModel],
        task_id: str,
    ) -> tuple[BaseModel, PMPMessage] | None:
        if not self._reservation_matches(state, agent_id, task_id):
            return None
        requests = {
            message.message_id: message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == agent_id
            and self._request_task_id(message) == task_id
        }
        for response in reversed(state.message_history):
            if (
                response.sender_agent_id != agent_id
                or response.receiver_agent_id != self.agent_id
                or response.message_type != MessageType.RESULT.value
                or response.parent_message_id not in requests
            ):
                continue
            try:
                result = output_schema.model_validate(response.payload)
            except Exception:
                continue
            return result, response
        return None

    def _stage_result_matches_state(
        self,
        state: PlaywrightWorkflowState,
        agent_id: str,
        result: BaseModel,
    ) -> bool:
        context = ProductionContext.model_validate(state.production_context)
        if agent_id == NARRATIVE_ARCHITECT_ID:
            return result.production_context_id == context.production_context_id
        if agent_id == SCRIPTWRITER_ID:
            return bool(
                state.narrative_blueprint
                and result.narrative_blueprint_id
                == state.narrative_blueprint.get("narrative_blueprint_id")
            )
        if agent_id == EVIDENCE_CITATION_EDITOR_ID:
            if not state.script_draft:
                return False
            script_id = state.script_draft.get("script_draft_id")
            return (
                result.citation_validated_script.source_script_draft_id == script_id
                and result.citation_manifest.script_draft_id == script_id
                and result.citation_validated_script.citation_manifest_id
                == result.citation_manifest.citation_manifest_id
            )
        return bool(
            state.citation_validated_script
            and result.citation_validated_script_id
            == state.citation_validated_script.get("citation_validated_script_id")
        )

    def _reservation_matches(
        self,
        state: PlaywrightWorkflowState,
        agent_id: str,
        task_id: str,
    ) -> bool:
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            return False
        path = self.provider_retry_store.reservation_path(
            provider_id=provider_id,
            workflow_id=state.workflow_id,
            task_id=task_id,
        )
        try:
            reservation = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return False
        return (
            reservation.get("workflow_id") == state.workflow_id
            and reservation.get("task_id") == task_id
            and reservation.get("agent_id") == agent_id
        )

    def _recoverable_stage_task_ids(
        self,
        state: PlaywrightWorkflowState,
        agent_id: str,
    ) -> list[str]:
        task_ids = [self._logical_task_id(state, agent_id)]
        if state.upstream_revision_count == 0 and state.revision_count == 0:
            task_ids.append(agent_id)
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if isinstance(provider_id, str):
            for base_task_id in list(task_ids):
                authorization = self.provider_retry_store.for_original_task(
                    workflow_id=state.workflow_id,
                    provider_id=provider_id,
                    original_task_id=base_task_id,
                )
                if (
                    authorization is not None
                    and authorization.retry_task_id not in task_ids
                ):
                    task_ids.append(authorization.retry_task_id)
                capability_authorization = (
                    self.provider_capability_repair_store.for_original_task(
                        workflow_id=state.workflow_id,
                        provider_id=provider_id,
                        original_task_id=base_task_id,
                    )
                )
                if (
                    capability_authorization is not None
                    and capability_authorization.repair_task_id not in task_ids
                ):
                    task_ids.append(capability_authorization.repair_task_id)
        return task_ids

    def _has_unanswered_stage_request(
        self,
        state: PlaywrightWorkflowState,
        agent_id: str,
    ) -> bool:
        task_ids = set(self._recoverable_stage_task_ids(state, agent_id))
        return any(
            self._has_unanswered_task_request(state, agent_id, task_id)
            for task_id in task_ids
        )

    def _has_stage_request(
        self,
        state: PlaywrightWorkflowState,
        agent_id: str,
    ) -> bool:
        task_ids = set(self._recoverable_stage_task_ids(state, agent_id))
        return any(
            message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == agent_id
            and self._request_task_id(message) in task_ids
            for message in state.message_history
        )

    @classmethod
    def _has_unanswered_task_request(
        cls,
        state: PlaywrightWorkflowState,
        agent_id: str,
        task_id: str,
    ) -> bool:
        child_parent_ids = {
            message.parent_message_id
            for message in state.message_history
            if message.parent_message_id is not None
        }
        return any(
            message.sender_agent_id == "playwright.manager"
            and message.receiver_agent_id == agent_id
            and cls._request_task_id(message) == task_id
            and message.message_id not in child_parent_ids
            for message in state.message_history
        )

    @staticmethod
    def _request_task_id(request: PMPMessage) -> str:
        task_id = request.payload.get("task_id")
        if isinstance(task_id, str) and task_id:
            return task_id
        # Playwright checkpoints written before cycle-aware logical identity
        # used the agent ID as the reservation task ID.
        return request.receiver_agent_id

    @classmethod
    def _latest_failed_provider_exchange(
        cls,
        state: PlaywrightWorkflowState,
    ) -> tuple[PMPMessage, PMPMessage]:
        for error_response in reversed(state.message_history):
            if (
                error_response.message_type != MessageType.ERROR.value
                or error_response.sender_agent_id not in AGENT_ORDER
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
                or request.sender_agent_id != "playwright.manager"
                or request.receiver_agent_id != error_response.sender_agent_id
            ):
                raise ValueError(
                    "Playwright Provider failure is not correlated to its saved request"
                )
            request_task_id = request.payload.get("task_id")
            error_task_id = error_response.payload.get("task_id")
            if request_task_id != error_task_id and not (
                request_task_id is None and error_task_id is None
            ):
                raise ValueError(
                    "Playwright Provider failure task identity is not correlated"
                )
            return request, error_response
        raise ValueError("No failed Playwright Provider exchange was found")

    @staticmethod
    def _retryable_error_class(error_response: PMPMessage) -> str | None:
        error_class = str(error_response.payload.get("error_class") or "")
        if error_class in {"RetryableAgentError", "ProviderResponseContractError"}:
            return error_class
        if (
            error_class == "PayloadValidationError"
            and isinstance(error_response.payload.get("invalid_payload"), dict)
            and isinstance(error_response.payload.get("validation_errors"), list)
            and bool(error_response.payload.get("validation_errors"))
        ):
            return error_class
        if (
            error_class == "NonRetryableAgentError"
            and error_response.payload.get("http_status") == 400
            and "invalid argument"
            in str(error_response.payload.get("message") or "").lower()
        ):
            return "ProviderRequestSchemaError"
        return None

    @staticmethod
    def _is_provider_capability_failure(error_response: PMPMessage) -> bool:
        error_class = str(error_response.payload.get("error_class") or "")
        http_status = error_response.payload.get("http_status")
        message = str(error_response.payload.get("message") or "").lower()
        return (
            error_class
            in {"ProviderCapabilityError", "NonRetryableAgentError"}
            and http_status == 404
            and "no endpoints found that can handle the requested parameters"
            in message
        )

    @staticmethod
    def _logical_task_id(
        state: PlaywrightWorkflowState,
        agent_id: str,
    ) -> str:
        stage_names = {
            NARRATIVE_ARCHITECT_ID: "narrative",
            SCRIPTWRITER_ID: "script",
            EVIDENCE_CITATION_EDITOR_ID: "citation",
            VISUAL_DIRECTOR_ID: "visual",
        }
        return (
            f"playwright_{stage_names[agent_id]}"
            f"_upstream_{state.upstream_revision_count}"
            f"_revision_{state.revision_count}"
        )

    @staticmethod
    def _latest_revision_context(state: PlaywrightWorkflowState) -> dict[str, Any] | None:
        if not state.revision_history:
            return None
        return state.revision_history[-1].model_dump(mode="json")

    async def _fail(self, state, message: str, progress_callback):
        state.status = PlaywrightStatus.FAILED
        state.current_agent_ids = []
        state.error = {"stage": "playwright", "message": message}
        self.repository.save(state)
        await self._emit(progress_callback, f"Playwright失敗: {message}")
        return state

    @staticmethod
    async def _emit(callback: ProgressCallback | None, message: str) -> None:
        if callback is not None:
            await callback(message)
