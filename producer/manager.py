from __future__ import annotations

import hashlib
import inspect
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

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
from common.models.revision import (
    HumanSelectionImpact,
    LayerId,
    RevisionArtifactRef,
    RevisionAuditEvent,
    RevisionAuditEventType,
    RevisionBudgetPolicy,
    RevisionControlState,
    RevisionControlPhase,
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
from common.provider_output_repair import (
    ProviderOutputRepairAuthorization,
    ProviderOutputRepairAuthorizationStore,
)
from common.retrieval_provider_retry import (
    RETRIEVAL_PROVIDER_RETRY_SUFFIX,
    RetrievalProviderRetryAuthorization,
    RetrievalProviderRetryAuthorizationStore,
    RetrievalProviderRetryStatus,
)
from common.validation import PMPValidator
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from producer.registry import ProducerRegistry
from producer.schemas.review import QualityReviewOutput
from producer.schemas.general_opinion import GeneralOpinionInput
from producer.agents.general_opinion_analyst import general_opinion_retrieval_plan
from producer.state import ProducerWorkflowState, RevisionRecord, utc_now
from producer.workflow import AGENT_ORDER, DISPLAY_NAMES
from retrieval.models import RetrievedContext, RetrievalStrategy
from storage.workflow_repository import WorkflowRepository
from storage.revision_exchange_repository import (
    RevisionBudgetExhausted,
    RevisionExchangeRepository,
)


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
        self.max_revisions = max_revisions
        self.pmp_validator = PMPValidator()
        self.rd_loader = rd_loader or registry.rd_loader
        self.provider_retry_store = ProviderRetryAuthorizationStore(repository.data_dir)
        self.provider_output_repair_store = ProviderOutputRepairAuthorizationStore(
            repository.data_dir
        )
        self.retrieval_provider_retry_store = (
            RetrievalProviderRetryAuthorizationStore(repository.data_dir)
        )
        self.revision_exchange = RevisionExchangeRepository(repository.data_dir)

    def authorize_retrieval_provider_retry(
        self,
        workflow_id: str,
    ) -> RetrievalProviderRetryAuthorization:
        """Authorize one synchronous replacement for a terminal Batch search."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Producer Retrieval retry is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        if state.status != WorkflowStatus.FAILED.value:
            raise ValueError("Producer must be FAILED before a Retrieval provider retry")
        start_index = self._recovery_start_index(state)
        agent_id = AGENT_ORDER[start_index]
        if agent_id != "producer.general_opinion_analyst":
            raise ValueError("Retrieval provider retry is limited to General Opinion")
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
            raise ValueError("No persisted General Opinion Retrieval failure was found")
        request = next(
            (
                message
                for message in reversed(state.message_history)
                if message.message_id == error_response.parent_message_id
            ),
            None,
        )
        error_message = str(error_response.payload.get("message") or "")
        failed_model_id = str(error_response.payload.get("model_id") or "")
        if (
            request is None
            or request.sender_agent_id != self.agent_id
            or request.receiver_agent_id != agent_id
            or request.message_type != MessageType.TASK.value
            or error_response.payload.get("provider") != "openrouter"
            or error_response.payload.get("error_class") != "NonRetryableAgentError"
            or not error_message.startswith(
                "OPENROUTER_BATCH_FAILED: terminal status="
            )
            or not failed_model_id.lower().endswith(":batch")
        ):
            raise ValueError(
                "Persisted Producer failure is not a correlated terminal Batch Retrieval failure"
            )
        agent = self.registry.get(agent_id)
        coordinator = agent.retrieval_coordinator
        if coordinator is None:
            raise ValueError("General Opinion has no Retrieval coordinator")
        retrieval_provider = coordinator.provider
        retrieval_provider_id = getattr(retrieval_provider, "provider_id", None)
        runtime_model_id = getattr(retrieval_provider, "model", None)
        if not isinstance(retrieval_provider_id, str) or not isinstance(
            runtime_model_id, str
        ):
            raise ValueError("Retrieval provider/model identity is unavailable")
        canonical = GeneralOpinionInput.model_validate(
            {
                "selected_topic": state.selected_topic,
                "revision_context": (
                    state.revision_history[-1].model_dump(mode="json")
                    if state.revision_history
                    else None
                ),
            }
        )
        original_task_id, query = general_opinion_retrieval_plan(canonical)
        original_retrieval_id = coordinator.retrieval_identity(
            workflow_id=workflow_id,
            task_id=original_task_id,
            agent_id=agent_id,
            strategy=RetrievalStrategy.GENERAL_OPINION,
        )
        retry_task_id = f"{original_task_id}{RETRIEVAL_PROVIDER_RETRY_SUFFIX}"
        retry_retrieval_id = coordinator.retrieval_identity(
            workflow_id=workflow_id,
            task_id=retry_task_id,
            agent_id=agent_id,
            strategy=RetrievalStrategy.GENERAL_OPINION,
        )
        return self.retrieval_provider_retry_store.authorize_once(
            workflow_id=workflow_id,
            retrieval_provider_id=retrieval_provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            original_retrieval_id=original_retrieval_id,
            retry_retrieval_id=retry_retrieval_id,
            source_error_message_id=error_response.message_id,
            source_error_class="NonRetryableAgentError",
            failed_model_id=failed_model_id,
            runtime_model_id=runtime_model_id,
            retrieval_strategy=RetrievalStrategy.GENERAL_OPINION.value,
            query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
            max_results=5,
        )

    async def retry_retrieval_provider(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ProducerWorkflowState:
        """Run only the failed General Opinion checkpoint with one new search identity."""

        authorization = self.authorize_retrieval_provider_retry(workflow_id)
        if authorization.status == RetrievalProviderRetryStatus.CONSUMED.value:
            raise ValueError(
                "The one-time Retrieval provider retry was already consumed"
            )
        await self._emit(
            progress_callback,
            "One-shot synchronous Retrieval retry authorized: "
            + authorization.retry_task_id,
        )
        state = self.repository.load(workflow_id)
        start_index = self._recovery_start_index(state)
        state.error = None
        state.completed_at = None
        self.repository.save(state)
        provider_task_id = (
            "producer.general_opinion_analyst.retrieval-retry."
            + authorization.authorization_id.replace("-", "")[:12]
        )
        return await self._run_from(
            state,
            start_index,
            progress_callback,
            provider_task_id_overrides={
                "producer.general_opinion_analyst": provider_task_id
            },
            retrieval_task_id_overrides={
                "producer.general_opinion_analyst": authorization.retry_task_id
            },
            stop_after_index=start_index,
        )

    def authorize_provider_retry(
        self,
        workflow_id: str,
    ) -> ProviderRetryAuthorization:
        """Authorize one repaired General Opinion reasoning request.

        The saved error must match either the historical Gemini schema failure
        or the batch-model/synchronous-endpoint transport mismatch.  Both are
        code-level request-contract failures, not ambiguous generation retries.
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
        failed_model_id = str(error_response.payload.get("model_id") or "")
        agent = self.registry.get(agent_id)
        provider_id = getattr(agent.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Producer agent provider has no stable logical provider ID")
        original_task_id = str(error_response.payload.get("task_id") or agent_id)
        original_reservation = self.provider_retry_store.reservation_path(
            provider_id=provider_id,
            workflow_id=workflow_id,
            task_id=original_task_id,
        )
        schema_rejection = (
            agent_id == "producer.general_opinion_analyst"
            and
            error_response.payload.get("http_status") == 400
            and "INVALID_ARGUMENT" in error_message
        )
        batch_transport_rejection = (
            error_response.payload.get("http_status") == 404
            and failed_model_id.lower().endswith(":batch")
            and "only available through the Batch API" in error_message
        )
        can_retry_failed_invocation = getattr(
            agent.provider,
            "can_retry_failed_invocation",
            None,
        )
        batch_terminal_rejection = (
            error_response.payload.get("error_class") == "NonRetryableAgentError"
            and failed_model_id.lower().endswith(":batch")
            and error_message.startswith(
                "OPENROUTER_BATCH_FAILED: terminal status="
            )
            and callable(can_retry_failed_invocation)
            and can_retry_failed_invocation(
                reservation_path=original_reservation,
                model_id=failed_model_id,
            )
        )
        if (
            request is None
            or request.sender_agent_id != self.agent_id
            or request.receiver_agent_id != agent_id
            or request.message_type != MessageType.TASK.value
            or error_response.payload.get("provider") != "openrouter"
            or not (
                schema_rejection
                or batch_transport_rejection
                or batch_terminal_rejection
            )
        ):
            raise ValueError(
                "Persisted Producer failure is not a correlated request-contract "
                "rejection"
            )
        return self.provider_retry_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=agent_id,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            # Manager-level correlation classifies this as a repaired request
            # contract failure even when the transport surfaced a generic
            # NonRetryableAgentError after asynchronous Batch validation.
            source_error_class="ProviderRequestSchemaError",
        )

    async def retry_provider_call(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ProducerWorkflowState:
        """Run exactly the failed Producer checkpoint, never downstream agents."""

        authorization = self.authorize_provider_retry(workflow_id)
        await self._emit(
            progress_callback,
            "Operator one-time Producer retry authorized: "
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
        revision_task_overrides, retrieval_task_overrides = (
            self._active_revision_task_overrides(state)
        )
        original_task_id = revision_task_overrides.get(agent_id, agent_id)
        provider_id = getattr(self.registry.get(agent_id).provider, "provider_id", None)
        task_overrides: dict[str, str] = dict(revision_task_overrides)
        if isinstance(provider_id, str):
            authorization = self.provider_retry_store.for_original_task(
                workflow_id=workflow_id,
                provider_id=provider_id,
                original_task_id=original_task_id,
            )
            original_reservation = self.provider_retry_store.reservation_path(
                provider_id=provider_id,
                workflow_id=workflow_id,
                task_id=original_task_id,
            )
            if (
                authorization is not None
                and authorization.status == ProviderRetryStatus.PENDING.value
            ):
                task_overrides[agent_id] = authorization.retry_task_id
            elif original_reservation.exists():
                can_resume = getattr(
                    self.registry.get(agent_id).provider,
                    "can_resume_invocation",
                    None,
                )
                model_id = self.registry.get(agent_id).model
                if not callable(can_resume) or not can_resume(
                    reservation_path=original_reservation,
                    model_id=model_id,
                ):
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
            retrieval_task_id_overrides=retrieval_task_overrides,
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

    def _activate_pending_upstream_revision(
        self,
        state: ProducerWorkflowState,
    ) -> ProducerWorkflowState:
        """Adopt one canonical Researcher request without invoking a Provider."""

        pending: list[tuple[PMPMessage, RevisionRequestV1]] = []
        for message in self.revision_exchange.list_requests(
            target_layer=LayerId.PRODUCER,
            workflow_id=state.workflow_id,
        ):
            request = self.revision_exchange.validator.validate_request_message(message)
            if (
                request.route == RevisionRoute.UPSTREAM.value
                and request.source_layer == LayerId.RESEARCHER.value
                and request.target_layer == LayerId.PRODUCER.value
                and request.revision_request_id
                not in state.revision_control.consumed_request_ids
            ):
                pending.append((message, request))
        if not pending:
            raise FileNotFoundError(
                f"No pending Researcher Revision Request exists for {state.workflow_id}"
            )
        if len(pending) > 1:
            raise ValueError("Multiple pending Producer Revision Requests require operator review")
        message, request = pending[0]
        if request.target_agent_ids != ["producer.research_planner"]:
            raise ValueError(
                "Researcher may request only producer.research_planner Revision"
            )
        if request.evidence_expansion_allowed or request.retrieval_allowed:
            raise ValueError(
                "Researcher plan-defect Revision cannot authorize Producer Retrieval"
            )
        if state.research_plan is None:
            raise ValueError("Producer has no current Research Plan for upstream Revision")
        plan_id = str(state.research_plan.get("research_plan_id") or "")
        expected_plan = next(
            (
                item
                for item in request.base_artifacts
                if item.artifact_type == "producer.research_plan"
            ),
            None,
        )
        if expected_plan is None or expected_plan.artifact_id != plan_id:
            raise ValueError("Researcher Revision Request has no matching Research Plan base")
        if expected_plan.sha256 != canonical_sha256(state.research_plan):
            raise ValueError("Researcher Revision Request is stale for the Research Plan")
        if not any(
            item.artifact_type == "researcher.research_report"
            for item in request.base_artifacts
        ):
            raise ValueError("Researcher Revision Request has no Research Report provenance")

        self._append_message_once(state, message)
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
        state.current_agent_id = None
        state.completed_at = None
        state.error = {
            "code": "UPSTREAM_REVISION_AUTHORIZATION_REQUIRED",
            "message": (
                "Researcher requested one Research Plan Revision; explicit Provider "
                "authorization is required"
            ),
        }
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"upstream_request_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PRODUCER,
                event_type=RevisionAuditEventType.REQUEST_CONSUMED,
                actor_id=self.agent_id,
                message_id=message.message_id,
                reason="Producer adopted the request and stopped at the authorization boundary",
            ),
        )
        self.repository.save(state)
        return state

    def authorize_revision(
        self,
        workflow_id: str,
        *,
        actor_id: str,
        actor_source: str,
        reason: str,
    ) -> RevisionExecutionAuthorization:
        """Persist one operator authorization without invoking any Provider."""

        state = self.repository.load(workflow_id)
        if state.revision_control.phase != RevisionControlPhase.AUTHORIZATION_REQUIRED.value:
            state = self._activate_pending_upstream_revision(state)
        if state.revision_control.phase != RevisionControlPhase.AUTHORIZATION_REQUIRED.value:
            raise ValueError("Producer has no revision awaiting operator authorization")
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
        return authorization

    async def revise(
        self,
        workflow_id: str,
        *,
        actor_id: str = "cli.operator",
        actor_source: str = "CLI",
        reason: str = "Operator authorized one Producer internal revision cycle",
        progress_callback: ProgressCallback | None = None,
    ) -> ProducerWorkflowState:
        """Authorize and execute exactly one saved Producer Revision plan."""

        state = self.repository.load(workflow_id)
        if state.revision_control.phase != RevisionControlPhase.AUTHORIZATION_REQUIRED.value:
            try:
                state = self._activate_pending_upstream_revision(state)
            except FileNotFoundError:
                if (
                    state.status == WorkflowStatus.COMPLETED.value
                    and state.revision_control.phase == RevisionControlPhase.COMPLETED.value
                ):
                    return state
                raise
        authorization = self.authorize_revision(
            workflow_id,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
        )
        state = self.repository.load(workflow_id)
        return await self._execute_active_revision(
            state,
            authorization=authorization,
            progress_callback=progress_callback,
        )

    async def _run_from(
        self,
        state: ProducerWorkflowState,
        start_index: int,
        progress_callback: ProgressCallback | None,
        *,
        provider_task_id_overrides: dict[str, str] | None = None,
        retrieval_task_id_overrides: dict[str, str] | None = None,
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
                retrieval_task_id=(retrieval_task_id_overrides or {}).get(agent_id),
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
        retrieval_task_id: str | None = None,
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
                    **(
                        {"retrieval_task_id": retrieval_task_id}
                        if retrieval_task_id is not None
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
            if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
                self._finalize_active_revision(
                    state,
                    review_response=response,
                    completed=True,
                    reason=review.reason,
                )
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
            if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
                self._finalize_active_revision(
                    state,
                    review_response=response,
                    completed=False,
                    reason=review.reason,
                )
            request = self._plan_internal_revision(state, response, review)
            await self._emit(
                progress_callback,
                "Quality Reviewer: revision_required → "
                f"{request.target_agent_ids[0]} (epoch {request.revision_epoch})",
            )
            if self.demo_safe_mode:
                state.revision_control.phase = RevisionControlPhase.AUTHORIZATION_REQUIRED
                state.status = WorkflowStatus.BLOCKED
                state.current_agent_id = None
                state.error = {
                    "message": (
                        "Demo Safe Mode stopped automatic Producer Revision; "
                        "an explicit operator revision command is required"
                    )
                }
                state.completed_at = None
                self.repository.save(state)
                return state
            authorization = self._create_revision_authorization(
                state,
                request,
                actor_id="producer.manager",
                actor_source="SYSTEM",
                reason="Safe Mode is disabled; runtime policy permits one automatic revision",
            )
            self.repository.save(state)
            return await self._execute_active_revision(
                state,
                authorization=authorization,
                progress_callback=progress_callback,
            )
        if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
            self._finalize_active_revision(
                state,
                review_response=response,
                completed=False,
                reason=review.reason,
            )
        return await self._fail(state, f"Quality Reviewer blocked workflow: {review.reason}", progress_callback)

    def _plan_internal_revision(
        self,
        state: ProducerWorkflowState,
        review_response: PMPMessage,
        review: QualityReviewOutput,
    ) -> RevisionRequestV1:
        if state.research_plan is None:
            raise ValueError("Producer Revision requires the reviewed research_plan")
        target = review.revision_target or ""
        if target not in AGENT_ORDER[:-1]:
            raise ValueError("Producer Revision target is not an executable specialist")

        revision_epoch = max(
            state.revision_count,
            state.revision_control.revision_epoch,
        ) + 1
        finding_id = f"producer_finding_{review_response.message_id.replace('-', '')}"
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.PRODUCER,
            target_layer=LayerId.PRODUCER,
            revision_epoch=revision_epoch,
            source_review_id=review_response.message_id,
            source_finding_ids=[finding_id],
        )
        plan_id = str(state.research_plan.get("research_plan_id") or "")
        if not plan_id:
            raise ValueError("Producer Revision base research_plan has no research_plan_id")
        retrieval_required = AGENT_ORDER.index(target) <= AGENT_ORDER.index(
            "producer.general_opinion_analyst"
        )
        request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.INTERNAL,
            source_layer=LayerId.PRODUCER,
            target_layer=LayerId.PRODUCER,
            revision_epoch=revision_epoch,
            root_revision_request_id=request_id,
            source_review_id=review_response.message_id,
            source_finding_ids=[finding_id],
            target_agent_ids=[target],
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="producer.research_plan",
                    artifact_id=plan_id,
                    sha256=canonical_sha256(state.research_plan),
                )
            ],
            required_actions=[review.required_action or review.reason],
            acceptance_conditions=[
                f"{finding_id} is explicitly resolved by a new Quality Review"
            ],
            evidence_expansion_allowed=retrieval_required,
            retrieval_allowed=retrieval_required,
            expected_human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            created_at=review_response.metadata.updated_at,
        )
        message_id = str(uuid5(NAMESPACE_URL, f"{request_id}:request-message"))
        revision_message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=review_response.message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id=target,
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Execute an audited Producer internal revision",
                    payload=request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="producer.manager",
                        previous_stage="producer.quality_reviewer",
                        next_stage=target,
                    ),
                    routing=PMPRouting(revision_target=target, reply_required=True),
                    metadata=PMPMetadata(
                        created_at=review_response.metadata.updated_at,
                        updated_at=review_response.metadata.updated_at,
                        status=MessageStatus.REVISION_REQUIRED,
                        extensions={"role_definition": state.role_definition_usage[-1]},
                    ),
                ).model_dump(mode="json"),
                "message_id": message_id,
            }
        )
        self.revision_exchange.create_internal_request_once(revision_message)
        self._append_message_once(state, revision_message)
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": RevisionControlPhase.PLANNED.value,
                "revision_epoch": revision_epoch,
                "active_request_id": request_id,
                "active_request_message_id": revision_message.message_id,
                "active_result_id": None,
                "root_revision_request_id": request_id,
                "parent_revision_request_id": None,
                "pending_request_ids": list(
                    dict.fromkeys(
                        [*state.revision_control.pending_request_ids, request_id]
                    )
                ),
            }
        )
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"request_written_{revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.PRODUCER,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=revision_message.message_id,
                artifact_ids=[plan_id],
                reason=review.reason,
                created_at=review_response.metadata.updated_at,
            ),
        )
        self.repository.save(state)
        return request

    def _create_revision_authorization(
        self,
        state: ProducerWorkflowState,
        request: RevisionRequestV1,
        *,
        actor_id: str,
        actor_source: str,
        reason: str,
    ) -> RevisionExecutionAuthorization:
        provider_tasks, _retrieval_tasks, retrieval_ids = (
            self._revision_execution_identities(state, request)
        )
        try:
            existing = self.revision_exchange.load_authorization(
                executing_layer=LayerId.PRODUCER,
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
                raise ValueError("Producer Revision is already authorized by a different actor")
            return existing

        authorization = RevisionExecutionAuthorization(
            authorization_id=(
                "revision_authorization_"
                + uuid5(NAMESPACE_URL, request.revision_request_id).hex
            ),
            workflow_id=state.workflow_id,
            revision_request_id=request.revision_request_id,
            executing_layer=LayerId.PRODUCER,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            max_provider_calls=len(provider_tasks),
            max_retrieval_calls=len(retrieval_ids),
        )
        self.revision_exchange.create_authorization_once(authorization)
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"authorization_created_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PRODUCER,
                event_type=RevisionAuditEventType.AUTHORIZATION_CREATED,
                actor_id=actor_id,
                reason=reason,
                created_at=authorization.created_at,
            ),
        )
        return authorization

    async def _execute_active_revision(
        self,
        state: ProducerWorkflowState,
        *,
        authorization: RevisionExecutionAuthorization,
        progress_callback: ProgressCallback | None,
    ) -> ProducerWorkflowState:
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        current_hashes = self._current_revision_artifact_hashes(state)
        owned_base_artifacts = [
            item
            for item in request.base_artifacts
            if item.artifact_type.startswith("producer.")
        ]
        self.revision_exchange.validator.validate_current_base_artifacts(
            request.model_copy(update={"base_artifacts": owned_base_artifacts}),
            current_hashes,
        )
        try:
            if request.route == RevisionRoute.INTERNAL.value:
                budget = self.revision_exchange.budget_store.consume(
                    policy=RevisionBudgetPolicy(
                        internal_limit=self.max_revisions,
                        upstream_limit=0,
                    ),
                    workflow_id=state.workflow_id,
                    layer=LayerId.PRODUCER,
                    route=RevisionRoute.INTERNAL,
                    revision_request_id=request.revision_request_id,
                )
            else:
                budget = self.revision_exchange.budget_store.for_request(
                    workflow_id=state.workflow_id,
                    layer=LayerId.RESEARCHER,
                    route=RevisionRoute.UPSTREAM,
                    revision_request_id=request.revision_request_id,
                )
                if budget is None:
                    raise RevisionBudgetExhausted(
                        "Researcher upstream Revision Request has no consumed budget slot"
                    )
        except RevisionBudgetExhausted as exc:
            state.revision_control.phase = RevisionControlPhase.BLOCKED
            state.status = WorkflowStatus.BLOCKED
            state.current_agent_id = None
            state.error = {"message": str(exc)}
            self._record_revision_audit(
                state,
                RevisionAuditEvent(
                    audit_event_id=f"budget_blocked_{request.revision_epoch}",
                    workflow_id=state.workflow_id,
                    revision_request_id=request.revision_request_id,
                    layer=LayerId.PRODUCER,
                    event_type=RevisionAuditEventType.BLOCKED,
                    actor_id=self.agent_id,
                    reason=str(exc),
                ),
            )
            self.repository.save(state)
            return state

        provider_tasks, retrieval_tasks, retrieval_ids = (
            self._revision_execution_identities(state, request)
        )
        consumed_authorization = self.revision_exchange.consume_authorization(
            authorization,
            provider_reservation_ids=list(provider_tasks.values()),
            retrieval_reservation_ids=retrieval_ids,
        )
        for event in (
            RevisionAuditEvent(
                audit_event_id=f"authorization_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PRODUCER,
                event_type=RevisionAuditEventType.AUTHORIZATION_CONSUMED,
                actor_id=authorization.actor_id,
                reservation_ids=[*provider_tasks.values(), *retrieval_ids],
                reason=authorization.reason,
                created_at=consumed_authorization.consumed_at,
            ),
            RevisionAuditEvent(
                audit_event_id=f"budget_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PRODUCER,
                event_type=RevisionAuditEventType.BUDGET_CONSUMED,
                actor_id=self.agent_id,
                reason=(
                    f"Producer {request.route} revision slot {budget.iteration}"
                ),
                created_at=budget.consumed_at,
            ),
            RevisionAuditEvent(
                audit_event_id=f"request_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PRODUCER,
                event_type=RevisionAuditEventType.REQUEST_CONSUMED,
                actor_id=self.agent_id,
                message_id=request_message.message_id,
                reason=f"Producer Manager began the saved {request.route} revision plan",
                created_at=budget.consumed_at,
            ),
        ):
            self._record_revision_audit(state, event)

        if not any(
            item.revision_request_id == request.revision_request_id
            for item in state.revision_history
        ):
            state.revision_history.append(
                RevisionRecord(
                    iteration=budget.iteration,
                    target_agent=request.target_agent_ids[0],
                    reason=state.review_result.get("reason", "") if state.review_result else "",
                    required_action=request.required_actions[0],
                    revision_request_id=request.revision_request_id,
                    source_finding_ids=request.source_finding_ids,
                    provider_task_ids=list(provider_tasks.values()),
                    retrieval_reservation_ids=retrieval_ids,
                )
            )
        if request.route == RevisionRoute.INTERNAL.value:
            state.revision_count = max(state.revision_count, budget.iteration)
        state.revision_control.phase = RevisionControlPhase.EXECUTING
        state.status = WorkflowStatus.REVISING
        state.error = None
        state.completed_at = None
        target = request.target_agent_ids[0]
        self._invalidate_from(state, target)
        self.repository.save(state)
        await self._emit(
            progress_callback,
            f"Producer {request.route} Revision {budget.iteration} authorized",
        )
        return await self._run_from(
            state,
            AGENT_ORDER.index(target),
            progress_callback,
            provider_task_id_overrides=provider_tasks,
            retrieval_task_id_overrides=retrieval_tasks,
        )

    def _finalize_active_revision(
        self,
        state: ProducerWorkflowState,
        *,
        review_response: PMPMessage,
        completed: bool,
        reason: str,
    ) -> None:
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        result_artifacts: list[RevisionArtifactRef] = []
        if state.research_plan is not None:
            plan_id = str(state.research_plan.get("research_plan_id") or "")
            if plan_id:
                result_artifacts.append(
                    RevisionArtifactRef(
                        artifact_type="producer.research_plan",
                        artifact_id=plan_id,
                        sha256=canonical_sha256(state.research_plan),
                    )
                )
        provider_tasks, _retrieval_tasks, retrieval_ids = (
            self._revision_execution_identities(state, request)
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
                    result_artifact_ids=[item.artifact_id for item in result_artifacts],
                )
                for finding_id in request.source_finding_ids
            ],
            human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            provider_reservation_ids=list(provider_tasks.values()),
            retrieval_reservation_ids=retrieval_ids,
            provider_call_count=len(provider_tasks),
            retrieval_call_count=len(retrieval_ids),
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
                    objective=f"Return the audited Producer {request.route} revision result",
                    payload=result.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="producer.manager",
                        previous_stage=request_message.receiver_agent_id,
                        next_stage="producer.quality_reviewer",
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
        self._append_message_once(state, result_message)
        self._record_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"result_written_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.PRODUCER,
                event_type=RevisionAuditEventType.RESULT_WRITTEN,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[item.artifact_id for item in result_artifacts],
                reservation_ids=[*provider_tasks.values(), *retrieval_ids],
                reason=reason,
                created_at=review_response.metadata.updated_at,
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

    def _revision_execution_identities(
        self,
        state: ProducerWorkflowState,
        request: RevisionRequestV1,
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        target_index = AGENT_ORDER.index(request.target_agent_ids[0])
        suffix = request.revision_request_id.rsplit("_", 1)[-1][:12]
        provider_tasks = {
            agent_id: f"{agent_id}.revision.{request.revision_epoch}.{suffix}"
            for agent_id in AGENT_ORDER[target_index:]
        }
        general_id = "producer.general_opinion_analyst"
        retrieval_tasks: dict[str, str] = {}
        retrieval_ids: list[str] = []
        if target_index <= AGENT_ORDER.index(general_id):
            retrieval_task_id = (
                f"general_opinion_revision_{request.revision_epoch}_{suffix}"
            )
            coordinator = self.registry.get(general_id).retrieval_coordinator
            if coordinator is None:
                raise ValueError(
                    "Producer Revision requiring General Opinion has no Retrieval coordinator"
                )
            retrieval_tasks[general_id] = retrieval_task_id
            retrieval_ids.append(
                coordinator.retrieval_identity(
                    workflow_id=state.workflow_id,
                    task_id=retrieval_task_id,
                    agent_id=general_id,
                    strategy=RetrievalStrategy.GENERAL_OPINION,
                )
            )
        return provider_tasks, retrieval_tasks, retrieval_ids

    def _active_revision_task_overrides(
        self,
        state: ProducerWorkflowState,
    ) -> tuple[dict[str, str], dict[str, str]]:
        if state.revision_control.phase != RevisionControlPhase.EXECUTING.value:
            return {}, {}
        request_message = self._active_revision_request_message(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        provider_tasks, retrieval_tasks, _retrieval_ids = (
            self._revision_execution_identities(state, request)
        )
        return provider_tasks, retrieval_tasks

    def _active_revision_request_message(
        self,
        state: ProducerWorkflowState,
    ) -> PMPMessage:
        message_id = state.revision_control.active_request_message_id
        request_id = state.revision_control.active_request_id
        if not message_id or not request_id:
            raise ValueError("Producer Revision control has no active request identity")
        message = next(
            (item for item in state.message_history if item.message_id == message_id),
            None,
        )
        if message is None:
            try:
                message = self.revision_exchange.load_internal_request(
                    layer=LayerId.PRODUCER,
                    workflow_id=state.workflow_id,
                    revision_request_id=request_id,
                )
            except FileNotFoundError:
                message = self.revision_exchange.load_request(
                    target_layer=LayerId.PRODUCER,
                    workflow_id=state.workflow_id,
                    revision_request_id=request_id,
                )
        request = self.revision_exchange.validator.validate_request_message(message)
        if request.revision_request_id != request_id:
            raise ValueError("Producer active Revision Request identity is inconsistent")
        return message

    @staticmethod
    def _current_revision_artifact_hashes(
        state: ProducerWorkflowState,
    ) -> dict[tuple[str, str], str]:
        if state.research_plan is None:
            return {}
        artifact_id = str(state.research_plan.get("research_plan_id") or "")
        if not artifact_id:
            return {}
        return {
            ("producer.research_plan", artifact_id): canonical_sha256(
                state.research_plan
            )
        }

    def _record_revision_audit(
        self,
        state: ProducerWorkflowState,
        event: RevisionAuditEvent,
    ) -> None:
        self.revision_exchange.create_audit_event_once(event)
        if event.audit_event_id not in state.revision_control.audit_event_ids:
            state.revision_control.audit_event_ids.append(event.audit_event_id)

    @staticmethod
    def _append_message_once(
        state: ProducerWorkflowState,
        message: PMPMessage,
    ) -> None:
        existing = next(
            (item for item in state.message_history if item.message_id == message.message_id),
            None,
        )
        if existing is None:
            state.message_history.append(message)
        elif existing != message:
            raise ValueError("Producer Revision message identity conflict")

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
