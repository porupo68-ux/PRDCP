from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5

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
)
from common.provider_runtime_model_repair import (
    RuntimeModelRepairAuthorizationStore,
    RuntimeModelRepairStatus,
)
from common.provider_runtime_output_repair import (
    RETRIEVAL_EXCERPT_HYDRATION_FAILURE,
    SOURCE_IDENTITY_CANONICALIZATION_FAILURE,
    SOURCE_PROVENANCE_OWNERSHIP_FAILURE,
    SOURCE_REDUNDANT_IDENTITY_HYDRATION_FAILURE,
    RuntimeAdapterRepairAuthorizationStore,
    RuntimeAdapterRepairStatus,
    RuntimeIdentityHydrationRepairAuthorizationStore,
    RuntimeModelOutputRepairAuthorizationStore,
    RuntimeModelOutputRepairStatus,
    RuntimeProvenanceHydrationRepairAuthorizationStore,
)
from common.retrieval_reconstruction import (
    RETRIEVAL_RECONSTRUCTION_SUFFIX,
    RetrievalReconstructionAuthorizationStore,
    RetrievalReconstructionStatus,
)
from common.validation import PMPValidator
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from producer.schemas.research_plan import ResearchPlan, ResearchTarget
from providers.openrouter_capabilities import (
    ModelCapabilityStatus,
    OpenRouterModelCapabilityClient,
)
from researcher.registry import ResearcherRegistry
from researcher.agents.base import canonical_country_from_url, canonical_source_label
from researcher.integrity_repair import (
    DuplicateTrackingPlan,
    apply_duplicate_tracking_plan,
    immutable_report_sha256,
    is_duplicate_tracking_finding,
    plan_duplicate_tracking_repair,
    relation_metadata_sha256,
)
from researcher.schemas.human_evidence import (
    AcceptedEvidenceGap,
    EvidenceFindingDisposition,
    EvidenceRevisionPlan,
    HumanActorSource,
    HumanEvidenceDecision,
    HumanEvidenceDecisionArtifact,
    HumanEvidenceDecisionType,
    HumanEvidenceGateSummary,
    HumanEvidenceIntegrityRepair,
    HumanEvidenceSourceReclassificationRepair,
    ResearchReportIntegrityRepair,
    ResearchSourceDuplicateTrackingRepair,
    ResearchFindingType,
)
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
from researcher.schemas.trace_ids import canonicalize_legacy_trace_ids
from researcher.state import (
    ExternalResearchRevisionRecord,
    ExternalRevisionCheckpoint,
    ResearchRevisionRecord,
    ResearcherWorkflowState,
    utc_now,
)
from researcher.workflow import DISPLAY_NAMES, QUALITY_REVIEWER_ID
from retrieval import RetrievedContext, RetrievalStrategy
from storage.researcher_workflow_repository import ResearcherWorkflowRepository
from storage.revision_exchange_repository import (
    RevisionBudgetExhausted,
    RevisionExchangeRepository,
)


ProgressCallback = Callable[[str], Awaitable[None] | None]
EXCERPT_CONTRACT_ERROR_PREFIX = (
    "OUTPUT_CONTRACT_ERROR: excerpt is absent from retrieval context for "
)
SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX = (
    "OUTPUT_CONTRACT_ERROR: ungrounded source identity metadata for "
)
OFFICIAL_INDUSTRY_REPAIR_HOSTS = {
    "cas.go.jp",
    "bunka.go.jp",
    "copyright.gov",
    "euipo.europa.eu",
    "bundesnetzagentur.de",
}
RECOGNIZED_NEWS_MEDIA_BY_HOST = {
    "xtech.nikkei.com": {
        "media_name": "日経クロステック",
        "article_type": "REPORTING",
    },
}
RECOGNIZED_NEWS_CONTEXT_MARKERS = ("報道", "ニュース", "news", "reporting")
SOURCE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.:-])source_[A-Za-z0-9_.:-]+(?![A-Za-z0-9_.:-])"
)
HARD_INTEGRITY_MARKERS = (
    "fabricated source",
    "unknown source_id",
    "url provenance mismatch",
    "invalid source identity",
    "schema violation",
    "pmp violation",
    "traceability corruption",
    "invalid canonical source category",
    "malformed payload",
    "cross-layer contract failure",
    "source_type分類",
    "カテゴリ分類",
)
EVIDENCE_SUFFICIENCY_MARKERS = (
    "不足",
    "未収集",
    "0件",
    "missing",
    "lack",
    "coverage",
    "additional research",
    "追加調査",
    "補強",
    "収集",
)


@dataclass(frozen=True)
class SavedRetrievalEvidence:
    context: RetrievedContext
    sha256: str
    retrieval_task_id: str
    source: str


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
        self.runtime_model_repair_store = RuntimeModelRepairAuthorizationStore(
            repository.data_dir
        )
        self.runtime_model_output_repair_store = (
            RuntimeModelOutputRepairAuthorizationStore(repository.data_dir)
        )
        self.runtime_adapter_repair_store = RuntimeAdapterRepairAuthorizationStore(
            repository.data_dir
        )
        self.runtime_identity_repair_store = (
            RuntimeIdentityHydrationRepairAuthorizationStore(repository.data_dir)
        )
        self.runtime_provenance_repair_store = (
            RuntimeProvenanceHydrationRepairAuthorizationStore(repository.data_dir)
        )
        self.retrieval_reconstruction_store = (
            RetrievalReconstructionAuthorizationStore(repository.data_dir)
        )
        self.revision_exchange = RevisionExchangeRepository(repository.data_dir)

    def inspect_runtime_model_recovery(self, workflow_id: str) -> dict[str, Any]:
        """Return a read-only task audit without authorizing or invoking providers."""

        state = self.repository.load(workflow_id)
        self._restore_results_from_message_history(state, persist=False)
        completed_task_ids = self._completed_research_task_ids(state)
        tasks: list[dict[str, Any]] = []
        for raw_task in state.research_tasks:
            task = ResearchTask.model_validate(raw_task)
            error = self._latest_initial_task_error(state, task)
            retrieval = self._saved_retrieval_evidence(state, task)
            runtime_model = self.registry.get(task.target_agent_id).model
            failed_model = error.payload.get("model_id") if error is not None else None
            blockers: list[str] = []
            if task.task_id in completed_task_ids:
                blockers.append("RESULT_ALREADY_SAVED")
            if error is None:
                blockers.append("MATCHING_PROVIDER_CAPABILITY_ERROR_NOT_FOUND")
            elif (
                error.payload.get("http_status") != 404
                or error.payload.get("error_class")
                not in {"ProviderCapabilityError", "NonRetryableAgentError"}
            ):
                blockers.append("FAILURE_IS_NOT_ENDPOINT_CAPABILITY_HTTP_404")
            if not isinstance(failed_model, str) or not failed_model:
                blockers.append("FAILED_MODEL_ID_MISSING")
            elif failed_model == runtime_model:
                blockers.append("RUNTIME_MODEL_HAS_NOT_CHANGED")
            if retrieval is None:
                blockers.append("SAVED_RETRIEVAL_CONTEXT_MISSING")
            tasks.append(
                {
                    "task_id": task.task_id,
                    "agent_id": task.target_agent_id,
                    "research_question_id": task.research_question_id,
                    "result_saved": task.task_id in completed_task_ids,
                    "source_error_message_id": (
                        error.message_id if error is not None else None
                    ),
                    "source_error_class": (
                        error.payload.get("error_class") if error is not None else None
                    ),
                    "source_http_status": (
                        error.payload.get("http_status") if error is not None else None
                    ),
                    "failed_model_id": failed_model,
                    "runtime_model_id": runtime_model,
                    "retrieval_id": (
                        retrieval.context.retrieval_id if retrieval is not None else None
                    ),
                    "retrieval_context_sha256": (
                        retrieval.sha256 if retrieval is not None else None
                    ),
                    "retrieval_task_id": (
                        retrieval.retrieval_task_id if retrieval is not None else None
                    ),
                    "retrieval_source": (
                        retrieval.source if retrieval is not None else None
                    ),
                    "eligible_before_capability_check": not blockers,
                    "blockers": blockers,
                }
            )
        return {
            "workflow_id": workflow_id,
            "state_status": str(state.status),
            "task_count": len(tasks),
            "completed_task_count": len(completed_task_ids),
            "eligible_before_capability_check": sum(
                bool(item["eligible_before_capability_check"]) for item in tasks
            ),
            "tasks": tasks,
        }

    def inspect_retrieval_reconstruction(self, workflow_id: str) -> dict[str, Any]:
        """Audit missing initial Retrieval checkpoints without changing storage."""

        state = self.repository.load(workflow_id)
        self._restore_results_from_message_history(state, persist=False)
        completed_task_ids = self._completed_research_task_ids(state)
        tasks: list[dict[str, Any]] = []
        for raw_task in state.research_tasks:
            task = ResearchTask.model_validate(raw_task)
            error = self._latest_initial_task_error(state, task)
            retrieval = self._saved_retrieval_evidence(state, task)
            runtime_model = self.registry.get(task.target_agent_id).model
            failed_model = error.payload.get("model_id") if error is not None else None
            blockers: list[str] = []
            if task.task_id in completed_task_ids:
                action = "RESULT_REUSE"
            elif retrieval is not None:
                action = "REASONING_READY"
            else:
                if error is None:
                    blockers.append("MATCHING_PROVIDER_CAPABILITY_ERROR_NOT_FOUND")
                elif (
                    error.payload.get("http_status") != 404
                    or error.payload.get("error_class")
                    not in {"ProviderCapabilityError", "NonRetryableAgentError"}
                ):
                    blockers.append("FAILURE_IS_NOT_ENDPOINT_CAPABILITY_HTTP_404")
                if not isinstance(failed_model, str) or not failed_model:
                    blockers.append("FAILED_MODEL_ID_MISSING")
                elif failed_model == runtime_model:
                    blockers.append("RUNTIME_MODEL_HAS_NOT_CHANGED")
                action = "BLOCKED" if blockers else "RECONSTRUCT_RETRIEVAL"
            tasks.append(
                {
                    "task_id": task.task_id,
                    "agent_id": task.target_agent_id,
                    "research_question_id": task.research_question_id,
                    "action": action,
                    "source_error_message_id": (
                        error.message_id if error is not None else None
                    ),
                    "source_error_class": (
                        error.payload.get("error_class") if error is not None else None
                    ),
                    "source_http_status": (
                        error.payload.get("http_status") if error is not None else None
                    ),
                    "failed_model_id": failed_model,
                    "runtime_model_id": runtime_model,
                    "retrieval_id": (
                        retrieval.context.retrieval_id if retrieval is not None else None
                    ),
                    "retrieval_context_sha256": (
                        retrieval.sha256 if retrieval is not None else None
                    ),
                    "retrieval_task_id": (
                        retrieval.retrieval_task_id if retrieval is not None else None
                    ),
                    "retrieval_source": (
                        retrieval.source if retrieval is not None else None
                    ),
                    "blockers": blockers,
                }
            )
        return {
            "workflow_id": workflow_id,
            "state_status": str(state.status),
            "task_count": len(tasks),
            "planned_retrieval_calls": sum(
                item["action"] == "RECONSTRUCT_RETRIEVAL" for item in tasks
            ),
            "planned_reasoning_calls": sum(
                item["action"] in {"RECONSTRUCT_RETRIEVAL", "REASONING_READY"}
                for item in tasks
            ),
            "tasks": tasks,
        }

    def inspect_runtime_output_repair(self, workflow_id: str) -> dict[str, Any]:
        """Audit the one historical excerpt-contract failure without writes."""

        state = self.repository.load(workflow_id)
        self._restore_results_from_message_history(state, persist=False)
        completed_task_ids = self._completed_research_task_ids(state)
        provider_id = getattr(self.registry.provider, "provider_id", None)
        tasks: list[dict[str, Any]] = []
        for raw_task in state.research_tasks:
            task = ResearchTask.model_validate(raw_task)
            blockers: list[str] = []
            if not isinstance(provider_id, str):
                blockers.append("PROVIDER_ID_MISSING")
                runtime_authorization = None
                output_authorization = None
            else:
                runtime_authorization = self.runtime_model_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=task.task_id,
                )
                output_authorization = (
                    self.runtime_model_output_repair_store.for_original_task(
                        workflow_id=workflow_id,
                        provider_id=provider_id,
                        original_task_id=task.task_id,
                    )
                )
            error = self._latest_runtime_model_repair_error(
                state,
                task,
                runtime_authorization.repair_task_id
                if runtime_authorization is not None
                else None,
            )
            retrieval = self._saved_retrieval_evidence(state, task)
            source_id: str | None = None
            if task.task_id in completed_task_ids:
                blockers.append("RESULT_ALREADY_SAVED")
            if runtime_authorization is None:
                blockers.append("CONSUMED_RUNTIME_REPAIR_NOT_FOUND")
            elif runtime_authorization.status != RuntimeModelRepairStatus.CONSUMED.value:
                blockers.append("RUNTIME_REPAIR_NOT_CONSUMED")
            if error is None:
                blockers.append("RUNTIME_REPAIR_ERROR_NOT_FOUND")
            else:
                message = error.payload.get("message")
                if (
                    error.payload.get("error_class") != "NonRetryableAgentError"
                    or not isinstance(message, str)
                    or not message.startswith(EXCERPT_CONTRACT_ERROR_PREFIX)
                ):
                    blockers.append("FAILURE_IS_NOT_EXCERPT_HYDRATION_CONTRACT")
                else:
                    source_id = message.removeprefix(
                        EXCERPT_CONTRACT_ERROR_PREFIX
                    ).strip()
            if retrieval is None:
                blockers.append("SAVED_RETRIEVAL_CONTEXT_MISSING")
            elif runtime_authorization is not None and (
                retrieval.context.retrieval_id != runtime_authorization.retrieval_id
                or retrieval.sha256
                != runtime_authorization.retrieval_context_sha256
            ):
                blockers.append("RETRIEVAL_IDENTITY_CHANGED")
            elif source_id is not None and source_id not in {
                source.source_id for source in retrieval.context.sources
            }:
                blockers.append("FAILED_SOURCE_ID_NOT_IN_RETRIEVAL")
            if (
                output_authorization is not None
                and output_authorization.status
                == RuntimeModelOutputRepairStatus.CONSUMED.value
            ):
                blockers.append("OUTPUT_REPAIR_ALREADY_CONSUMED")
            tasks.append(
                {
                    "task_id": task.task_id,
                    "agent_id": task.target_agent_id,
                    "runtime_model_id": self.registry.get(task.target_agent_id).model,
                    "source_error_message_id": (
                        error.message_id if error is not None else None
                    ),
                    "source_error_class": (
                        error.payload.get("error_class") if error is not None else None
                    ),
                    "failed_source_id": source_id,
                    "retrieval_id": (
                        retrieval.context.retrieval_id if retrieval is not None else None
                    ),
                    "retrieval_context_sha256": (
                        retrieval.sha256 if retrieval is not None else None
                    ),
                    "retrieval_task_id": (
                        retrieval.retrieval_task_id if retrieval is not None else None
                    ),
                    "eligible": not blockers,
                    "blockers": blockers,
                }
            )
        return {
            "workflow_id": workflow_id,
            "state_status": str(state.status),
            "eligible_count": sum(item["eligible"] for item in tasks),
            "planned_output_repair_calls": sum(item["eligible"] for item in tasks),
            "tasks": tasks,
        }

    def inspect_runtime_adapter_repair(self, workflow_id: str) -> dict[str, Any]:
        """Audit one identity-canonicalization failure without changing storage."""

        state = self.repository.load(workflow_id)
        self._restore_results_from_message_history(state, persist=False)
        completed_task_ids = self._completed_research_task_ids(state)
        provider_id = getattr(self.registry.provider, "provider_id", None)
        tasks: list[dict[str, Any]] = []
        for raw_task in state.research_tasks:
            task = ResearchTask.model_validate(raw_task)
            blockers: list[str] = []
            if not isinstance(provider_id, str):
                blockers.append("PROVIDER_ID_MISSING")
                predecessor = None
                authorization = None
            else:
                predecessor = (
                    self.runtime_model_output_repair_store.for_original_task(
                        workflow_id=workflow_id,
                        provider_id=provider_id,
                        original_task_id=task.task_id,
                    )
                )
                authorization = self.runtime_adapter_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=task.task_id,
                )
            error = self._latest_runtime_model_repair_error(
                state,
                task,
                predecessor.output_repair_task_id
                if predecessor is not None
                else None,
            )
            retrieval = self._saved_retrieval_evidence(state, task)
            source_id: str | None = None
            if task.task_id in completed_task_ids:
                blockers.append("RESULT_ALREADY_SAVED")
            if predecessor is None:
                blockers.append("CONSUMED_OUTPUT_REPAIR_NOT_FOUND")
            elif predecessor.status != RuntimeModelOutputRepairStatus.CONSUMED.value:
                blockers.append("OUTPUT_REPAIR_NOT_CONSUMED")
            elif predecessor.model_id != self.registry.get(task.target_agent_id).model:
                blockers.append("RUNTIME_MODEL_CHANGED_SINCE_OUTPUT_REPAIR")
            if error is None:
                blockers.append("OUTPUT_REPAIR_ERROR_NOT_FOUND")
            else:
                message = error.payload.get("message")
                if (
                    error.payload.get("error_class") != "NonRetryableAgentError"
                    or not isinstance(message, str)
                    or not message.startswith(SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX)
                ):
                    blockers.append("FAILURE_IS_NOT_SOURCE_IDENTITY_CONTRACT")
                else:
                    identity_detail = message.removeprefix(
                        SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX
                    )
                    source_id = identity_detail.split(":", 1)[0].strip()
            if retrieval is None:
                blockers.append("SAVED_RETRIEVAL_CONTEXT_MISSING")
            elif predecessor is not None and (
                retrieval.context.retrieval_id != predecessor.retrieval_id
                or retrieval.sha256 != predecessor.retrieval_context_sha256
            ):
                blockers.append("RETRIEVAL_IDENTITY_CHANGED")
            elif source_id is not None and source_id not in {
                source.source_id for source in retrieval.context.sources
            }:
                blockers.append("FAILED_SOURCE_ID_NOT_IN_RETRIEVAL")
            if (
                authorization is not None
                and authorization.status == RuntimeAdapterRepairStatus.CONSUMED.value
            ):
                blockers.append("ADAPTER_REPAIR_ALREADY_CONSUMED")
            tasks.append(
                {
                    "task_id": task.task_id,
                    "agent_id": task.target_agent_id,
                    "runtime_model_id": self.registry.get(task.target_agent_id).model,
                    "source_error_message_id": (
                        error.message_id if error is not None else None
                    ),
                    "source_error_class": (
                        error.payload.get("error_class") if error is not None else None
                    ),
                    "failed_source_id": source_id,
                    "retrieval_id": (
                        retrieval.context.retrieval_id if retrieval is not None else None
                    ),
                    "retrieval_context_sha256": (
                        retrieval.sha256 if retrieval is not None else None
                    ),
                    "retrieval_task_id": (
                        retrieval.retrieval_task_id if retrieval is not None else None
                    ),
                    "eligible": not blockers,
                    "blockers": blockers,
                }
            )
        return {
            "workflow_id": workflow_id,
            "state_status": str(state.status),
            "eligible_count": sum(item["eligible"] for item in tasks),
            "planned_adapter_repair_calls": sum(item["eligible"] for item in tasks),
            "tasks": tasks,
        }

    def inspect_runtime_identity_repair(self, workflow_id: str) -> dict[str, Any]:
        """Audit one redundant composite-identity failure without writes."""

        state = self.repository.load(workflow_id)
        self._restore_results_from_message_history(state, persist=False)
        completed_task_ids = self._completed_research_task_ids(state)
        provider_id = getattr(self.registry.provider, "provider_id", None)
        tasks: list[dict[str, Any]] = []
        for raw_task in state.research_tasks:
            task = ResearchTask.model_validate(raw_task)
            blockers: list[str] = []
            if not isinstance(provider_id, str):
                blockers.append("PROVIDER_ID_MISSING")
                predecessor = None
                authorization = None
            else:
                predecessor = self.runtime_adapter_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=task.task_id,
                )
                authorization = self.runtime_identity_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=task.task_id,
                )
            error = self._latest_runtime_model_repair_error(
                state,
                task,
                predecessor.repair_task_id if predecessor is not None else None,
            )
            retrieval = self._saved_retrieval_evidence(state, task)
            source_id: str | None = None
            identity_detail: str | None = None
            if task.task_id in completed_task_ids:
                blockers.append("RESULT_ALREADY_SAVED")
            if predecessor is None:
                blockers.append("CONSUMED_ADAPTER_REPAIR_NOT_FOUND")
            elif predecessor.status != RuntimeAdapterRepairStatus.CONSUMED.value:
                blockers.append("ADAPTER_REPAIR_NOT_CONSUMED")
            elif predecessor.model_id != self.registry.get(task.target_agent_id).model:
                blockers.append("RUNTIME_MODEL_CHANGED_SINCE_ADAPTER_REPAIR")
            if error is None:
                blockers.append("ADAPTER_REPAIR_ERROR_NOT_FOUND")
            else:
                message = error.payload.get("message")
                if (
                    error.payload.get("error_class") != "NonRetryableAgentError"
                    or not isinstance(message, str)
                    or not message.startswith(SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX)
                ):
                    blockers.append("FAILURE_IS_NOT_SOURCE_IDENTITY_CONTRACT")
                else:
                    remainder = message.removeprefix(
                        SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX
                    )
                    source_id, separator, identity_detail = remainder.partition(":")
                    source_id = source_id.strip()
                    identity_detail = identity_detail.strip()
                    if not separator or not any(
                        marker in identity_detail for marker in ("/", "／", "|", "｜")
                    ):
                        blockers.append("FAILURE_IS_NOT_COMPOSITE_IDENTITY")
            if retrieval is None:
                blockers.append("SAVED_RETRIEVAL_CONTEXT_MISSING")
            elif predecessor is not None and (
                retrieval.context.retrieval_id != predecessor.retrieval_id
                or retrieval.sha256 != predecessor.retrieval_context_sha256
            ):
                blockers.append("RETRIEVAL_IDENTITY_CHANGED")
            elif source_id is not None and source_id not in {
                source.source_id for source in retrieval.context.sources
            }:
                blockers.append("FAILED_SOURCE_ID_NOT_IN_RETRIEVAL")
            if (
                authorization is not None
                and authorization.status == RuntimeAdapterRepairStatus.CONSUMED.value
            ):
                blockers.append("IDENTITY_REPAIR_ALREADY_CONSUMED")
            tasks.append(
                {
                    "task_id": task.task_id,
                    "agent_id": task.target_agent_id,
                    "runtime_model_id": self.registry.get(task.target_agent_id).model,
                    "source_error_message_id": (
                        error.message_id if error is not None else None
                    ),
                    "source_error_class": (
                        error.payload.get("error_class") if error is not None else None
                    ),
                    "failed_source_id": source_id,
                    "failed_identity_detail": identity_detail,
                    "retrieval_id": (
                        retrieval.context.retrieval_id if retrieval is not None else None
                    ),
                    "retrieval_context_sha256": (
                        retrieval.sha256 if retrieval is not None else None
                    ),
                    "retrieval_task_id": (
                        retrieval.retrieval_task_id if retrieval is not None else None
                    ),
                    "eligible": not blockers,
                    "blockers": blockers,
                }
            )
        return {
            "workflow_id": workflow_id,
            "state_status": str(state.status),
            "eligible_count": sum(item["eligible"] for item in tasks),
            "planned_identity_repair_calls": sum(item["eligible"] for item in tasks),
            "tasks": tasks,
        }

    def inspect_runtime_provenance_repair(self, workflow_id: str) -> dict[str, Any]:
        """Audit one Provider-owned provenance failure without writes."""

        state = self.repository.load(workflow_id)
        self._restore_results_from_message_history(state, persist=False)
        completed_task_ids = self._completed_research_task_ids(state)
        provider_id = getattr(self.registry.provider, "provider_id", None)
        tasks: list[dict[str, Any]] = []
        for raw_task in state.research_tasks:
            task = ResearchTask.model_validate(raw_task)
            blockers: list[str] = []
            if not isinstance(provider_id, str):
                blockers.append("PROVIDER_ID_MISSING")
                predecessor = None
                authorization = None
            else:
                predecessor = self.runtime_identity_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=task.task_id,
                )
                authorization = self.runtime_provenance_repair_store.for_original_task(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    original_task_id=task.task_id,
                )
            error = self._latest_runtime_model_repair_error(
                state,
                task,
                predecessor.repair_task_id if predecessor is not None else None,
            )
            retrieval = self._saved_retrieval_evidence(state, task)
            source_id: str | None = None
            identity_detail: str | None = None
            if task.task_id in completed_task_ids:
                blockers.append("RESULT_ALREADY_SAVED")
            if predecessor is None:
                blockers.append("CONSUMED_IDENTITY_REPAIR_NOT_FOUND")
            elif predecessor.status != RuntimeAdapterRepairStatus.CONSUMED.value:
                blockers.append("IDENTITY_REPAIR_NOT_CONSUMED")
            elif predecessor.model_id != self.registry.get(task.target_agent_id).model:
                blockers.append("RUNTIME_MODEL_CHANGED_SINCE_IDENTITY_REPAIR")
            if error is None:
                blockers.append("IDENTITY_REPAIR_ERROR_NOT_FOUND")
            else:
                message = error.payload.get("message")
                if (
                    error.payload.get("error_class") != "NonRetryableAgentError"
                    or not isinstance(message, str)
                    or not message.startswith(SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX)
                ):
                    blockers.append("FAILURE_IS_NOT_SOURCE_IDENTITY_CONTRACT")
                else:
                    remainder = message.removeprefix(
                        SOURCE_IDENTITY_CONTRACT_ERROR_PREFIX
                    )
                    source_id, separator, identity_detail = remainder.partition(":")
                    source_id = source_id.strip()
                    identity_detail = identity_detail.strip()
                    if not separator or not source_id or not identity_detail:
                        blockers.append("SOURCE_IDENTITY_FAILURE_DETAIL_MISSING")
            if retrieval is None:
                blockers.append("SAVED_RETRIEVAL_CONTEXT_MISSING")
            elif predecessor is not None and (
                retrieval.context.retrieval_id != predecessor.retrieval_id
                or retrieval.sha256 != predecessor.retrieval_context_sha256
            ):
                blockers.append("RETRIEVAL_IDENTITY_CHANGED")
            elif source_id is not None and source_id not in {
                source.source_id for source in retrieval.context.sources
            }:
                blockers.append("FAILED_SOURCE_ID_NOT_IN_RETRIEVAL")
            if (
                authorization is not None
                and authorization.status == RuntimeAdapterRepairStatus.CONSUMED.value
            ):
                blockers.append("PROVENANCE_REPAIR_ALREADY_CONSUMED")
            tasks.append(
                {
                    "task_id": task.task_id,
                    "agent_id": task.target_agent_id,
                    "runtime_model_id": self.registry.get(task.target_agent_id).model,
                    "source_error_message_id": (
                        error.message_id if error is not None else None
                    ),
                    "source_error_class": (
                        error.payload.get("error_class") if error is not None else None
                    ),
                    "failed_source_id": source_id,
                    "failed_identity_detail": identity_detail,
                    "retrieval_id": (
                        retrieval.context.retrieval_id if retrieval is not None else None
                    ),
                    "retrieval_context_sha256": (
                        retrieval.sha256 if retrieval is not None else None
                    ),
                    "retrieval_task_id": (
                        retrieval.retrieval_task_id if retrieval is not None else None
                    ),
                    "eligible": not blockers,
                    "blockers": blockers,
                }
            )
        return {
            "workflow_id": workflow_id,
            "state_status": str(state.status),
            "eligible_count": sum(item["eligible"] for item in tasks),
            "planned_provenance_repair_calls": sum(item["eligible"] for item in tasks),
            "tasks": tasks,
        }

    async def recover_runtime_model_drift(
        self,
        workflow_id: str,
        *,
        capability_client: OpenRouterModelCapabilityClient | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        """Repair only incomplete initial tasks using their saved Retrieval contexts."""

        state = self.repository.load(workflow_id)
        if state.status not in {
            WorkflowStatus.FAILED.value,
            WorkflowStatus.PARTIALLY_COMPLETED.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError(
                "Runtime model repair requires a failed or incomplete Researcher workflow"
            )
        if state.research_report is not None:
            raise ValueError("Runtime model repair is only for pre-integration task failures")
        restored = self._restore_results_from_message_history(state, persist=False)
        if restored:
            self.repository.save(state)
        audit = self.inspect_runtime_model_recovery(workflow_id)
        candidates = [
            item
            for item in audit["tasks"]
            if item["eligible_before_capability_check"]
        ]
        if not candidates:
            if audit["completed_task_count"] == audit["task_count"] and audit["task_count"]:
                state = self.repository.load(workflow_id)
                self._restore_results_from_message_history(state, persist=False)
                state.error = None
                state.completed_at = None
                self.repository.save(state)
                return await self._integrate_and_review(state, progress_callback)
            missing = [
                item["task_id"]
                for item in audit["tasks"]
                if "SAVED_RETRIEVAL_CONTEXT_MISSING" in item["blockers"]
            ]
            suffix = f" Missing Retrieval Context: {', '.join(missing)}." if missing else ""
            raise ValueError(
                "RUNTIME_MODEL_REPAIR_BLOCKED: no eligible incomplete task."
                f"{suffix} Automatic retrieval is forbidden."
            )

        provider = self.registry.get(candidates[0]["agent_id"]).provider
        provider_id = getattr(provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Runtime model repair requires a stable provider ID")
        if capability_client is None:
            if provider_id != "openrouter":
                raise ValueError(
                    "Runtime model repair capability evidence is required for non-OpenRouter tests"
                )
            capability_client = OpenRouterModelCapabilityClient(
                base_url=getattr(provider, "base_url", "https://openrouter.ai/api/v1")
            )

        task_by_id = {
            task.task_id: task
            for task in (
                ResearchTask.model_validate(raw_task) for raw_task in state.research_tasks
            )
        }
        for index, item in enumerate(candidates, start=1):
            # Reload before every paid call so a post-commit save exception or a
            # concurrent operator recovery cannot repeat an already saved result.
            state = self.repository.load(workflow_id)
            self._restore_results_from_message_history(state, persist=False)
            if item["task_id"] in self._completed_research_task_ids(state):
                continue
            task = task_by_id[item["task_id"]]
            capability = capability_client.inspect(item["runtime_model_id"])
            if capability.status is not ModelCapabilityStatus.COMPATIBLE:
                raise ValueError(
                    "RUNTIME_MODEL_REPAIR_BLOCKED: runtime model capability is "
                    f"{capability.status.value} for {task.target_agent_id}: "
                    f"{capability.reason}"
                )
            authorization = self.runtime_model_repair_store.authorize_once(
                workflow_id=workflow_id,
                provider_id=provider_id,
                agent_id=task.target_agent_id,
                original_task_id=task.task_id,
                source_error_message_id=item["source_error_message_id"],
                source_error_class=item["source_error_class"],
                source_http_status=item["source_http_status"],
                failed_model_id=item["failed_model_id"],
                runtime_model_id=item["runtime_model_id"],
                capability_status=capability.status.value,
                capability_reason=capability.reason,
                retrieval_id=item["retrieval_id"],
                retrieval_context_sha256=item["retrieval_context_sha256"],
            )
            succeeded = await self._execute_runtime_model_repair_task(
                state,
                task,
                repair_task_id=authorization.repair_task_id,
                retrieval_task_id=item["retrieval_task_id"],
                runtime_model=item["runtime_model_id"],
                progress_callback=progress_callback,
                index=index,
                total=len(candidates),
            )
            if not succeeded:
                return await self._fail(
                    state,
                    f"Runtime model repair failed for {task.task_id}; repair-of-repair is blocked",
                    progress_callback,
                )

        state = self.repository.load(workflow_id)
        self._restore_results_from_message_history(state, persist=False)
        all_task_ids = {task.task_id for task in task_by_id.values()}
        missing = sorted(all_task_ids - self._completed_research_task_ids(state))
        if missing:
            raise ValueError(
                "RUNTIME_MODEL_REPAIR_BLOCKED: incomplete tasks remain: "
                + ", ".join(missing)
            )
        state.error = None
        state.completed_at = None
        self.repository.save(state)
        return await self._integrate_and_review(state, progress_callback)

    async def reconstruct_missing_retrieval_and_recover(
        self,
        workflow_id: str,
        *,
        capability_client: OpenRouterModelCapabilityClient | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        """Rebuild missing Retrieval checkpoints, then run Cycle 032 recovery.

        Each search uses a new, auditable identity and a consumed-before-call
        authorization.  The method is sequential and stops at the first failure,
        so saved contexts and ResearchResults are reused on a later invocation.
        """

        state = self.repository.load(workflow_id)
        if state.status not in {
            WorkflowStatus.FAILED.value,
            WorkflowStatus.PARTIALLY_COMPLETED.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError(
                "Retrieval reconstruction requires a failed or incomplete Researcher workflow"
            )
        if state.research_report is not None:
            raise ValueError(
                "Retrieval reconstruction is only for pre-integration task failures"
            )

        audit = self.inspect_retrieval_reconstruction(workflow_id)
        if not audit["tasks"]:
            raise ValueError("RETRIEVAL_RECONSTRUCTION_BLOCKED: workflow has no tasks")
        blocked = [item for item in audit["tasks"] if item["action"] == "BLOCKED"]
        if blocked:
            details = ", ".join(
                f"{item['task_id']}:{'/'.join(item['blockers'])}" for item in blocked
            )
            raise ValueError(f"RETRIEVAL_RECONSTRUCTION_BLOCKED: {details}")

        candidates = [
            item
            for item in audit["tasks"]
            if item["action"] == "RECONSTRUCT_RETRIEVAL"
        ]
        task_by_id = {
            task.task_id: task
            for task in (
                ResearchTask.model_validate(raw_task) for raw_task in state.research_tasks
            )
        }
        reasoning_provider = self.registry.provider
        failed_provider_id = getattr(reasoning_provider, "provider_id", None)
        if not isinstance(failed_provider_id, str):
            raise ValueError("Retrieval reconstruction requires a stable failed provider ID")

        first_agent = self.registry.get(
            candidates[0]["agent_id"] if candidates else audit["tasks"][0]["agent_id"]
        )
        coordinator = first_agent.retrieval_coordinator
        if coordinator is None:
            raise ValueError("Retrieval reconstruction requires a Retrieval provider")
        retrieval_provider = coordinator.provider
        retrieval_provider_id = getattr(retrieval_provider, "provider_id", None)
        if not isinstance(retrieval_provider_id, str):
            raise ValueError("Retrieval reconstruction requires a stable Retrieval provider ID")
        retrieval_model_id = str(
            getattr(retrieval_provider, "model", None) or retrieval_provider_id
        )

        if capability_client is None:
            if failed_provider_id != "openrouter":
                raise ValueError(
                    "Retrieval reconstruction capability evidence is required for "
                    "non-OpenRouter tests"
                )
            capability_client = OpenRouterModelCapabilityClient(
                base_url=getattr(
                    reasoning_provider,
                    "base_url",
                    "https://openrouter.ai/api/v1",
                )
            )

        for index, item in enumerate(candidates, start=1):
            # Re-audit immediately before each paid search.  This observes a
            # context saved by another operator and avoids a duplicate call.
            current_audit = self.inspect_retrieval_reconstruction(workflow_id)
            current = next(
                entry
                for entry in current_audit["tasks"]
                if entry["task_id"] == item["task_id"]
            )
            if current["action"] != "RECONSTRUCT_RETRIEVAL":
                if current["action"] in {"REASONING_READY", "RESULT_REUSE"}:
                    continue
                raise ValueError(
                    "RETRIEVAL_RECONSTRUCTION_BLOCKED: task eligibility changed for "
                    + item["task_id"]
                )

            task = task_by_id[item["task_id"]]
            agent = self.registry.get(task.target_agent_id)
            capability = capability_client.inspect(item["runtime_model_id"])
            if capability.status is not ModelCapabilityStatus.COMPATIBLE:
                raise ValueError(
                    "RETRIEVAL_RECONSTRUCTION_BLOCKED: runtime model capability is "
                    f"{capability.status.value} for {task.target_agent_id}: "
                    f"{capability.reason}"
                )
            strategy, query = agent.retrieval_request(task)
            reconstruction_task_id = (
                f"{task.task_id}{RETRIEVAL_RECONSTRUCTION_SUFFIX}"
            )
            retrieval_id = coordinator._retrieval_id(
                workflow_id,
                reconstruction_task_id,
                task.target_agent_id,
                strategy.value,
            )
            authorization = self.retrieval_reconstruction_store.authorize_once(
                workflow_id=workflow_id,
                failed_provider_id=failed_provider_id,
                retrieval_provider_id=retrieval_provider_id,
                retrieval_model_id=retrieval_model_id,
                agent_id=task.target_agent_id,
                original_task_id=task.task_id,
                source_error_message_id=item["source_error_message_id"],
                source_error_class=item["source_error_class"],
                source_http_status=item["source_http_status"],
                failed_model_id=item["failed_model_id"],
                runtime_model_id=item["runtime_model_id"],
                research_question_id=task.research_question_id,
                retrieval_strategy=strategy.value,
                query_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
                max_results=task.max_sources,
                retrieval_id=retrieval_id,
            )

            if authorization.status == RetrievalReconstructionStatus.CONSUMED.value:
                # This is the crash-after-context-write checkpoint.  The hash is
                # reconciled without invoking search again.  Missing context is
                # ambiguous and therefore remains fail-closed.
                self.retrieval_reconstruction_store.record_context(authorization)
            else:
                consumed_authorization = authorization

                def consume_before_search(path, actual_retrieval_id):
                    nonlocal consumed_authorization
                    if actual_retrieval_id != authorization.retrieval_id:
                        raise ValueError("Retrieval reconstruction identity changed")
                    consumed_authorization = (
                        self.retrieval_reconstruction_store.consume(
                            authorization,
                            reservation_path=path,
                        )
                    )

                timeout_seconds = RoleDefinitionExtractor().extract_runtime_config(
                    self.rd_loader.load(task.target_agent_id)
                ).timeout_seconds
                await agent.prepare_retrieval_context(
                    task,
                    workflow_id=workflow_id,
                    retrieval_task_id=reconstruction_task_id,
                    timeout_seconds=timeout_seconds,
                    before_provider_call=consume_before_search,
                )
                self.retrieval_reconstruction_store.record_context(
                    consumed_authorization
                )
            await self._emit(
                progress_callback,
                f"[{index}/{len(candidates)}] reconstructed Retrieval for {task.task_id}",
            )

        # The existing Cycle 032 path remains authoritative for reasoning,
        # task-result persistence, integration and Quality Review.
        return await self.recover_runtime_model_drift(
            workflow_id,
            capability_client=capability_client,
            progress_callback=progress_callback,
        )

    async def recover_runtime_output_contract(
        self,
        workflow_id: str,
        *,
        capability_client: OpenRouterModelCapabilityClient | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        """Repair one excerpt-adapter failure, then resume untouched tasks."""

        state = self.repository.load(workflow_id)
        if state.status not in {
            WorkflowStatus.FAILED.value,
            WorkflowStatus.PARTIALLY_COMPLETED.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError(
                "Runtime output repair requires a failed or incomplete Researcher workflow"
            )
        if state.research_report is not None:
            raise ValueError("Runtime output repair is only for pre-integration failures")
        restored = self._restore_results_from_message_history(state, persist=False)
        if restored:
            self.repository.save(state)

        audit = self.inspect_runtime_output_repair(workflow_id)
        candidates = [item for item in audit["tasks"] if item["eligible"]]
        if len(candidates) != 1:
            details = ", ".join(
                f"{item['task_id']}:{'/'.join(item['blockers'])}"
                for item in audit["tasks"]
                if item["blockers"]
            )
            raise ValueError(
                "RUNTIME_OUTPUT_REPAIR_BLOCKED: exactly one eligible excerpt-contract "
                f"failure is required; found {len(candidates)}. {details}"
            )
        item = candidates[0]
        task = next(
            ResearchTask.model_validate(raw_task)
            for raw_task in state.research_tasks
            if raw_task.get("task_id") == item["task_id"]
        )
        provider_id = getattr(self.registry.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Runtime output repair requires a stable provider ID")
        authorization = self.runtime_model_output_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=task.target_agent_id,
            original_task_id=task.task_id,
            source_error_message_id=item["source_error_message_id"],
            source_error_class=item["source_error_class"],
            model_id=item["runtime_model_id"],
            retrieval_id=item["retrieval_id"],
            retrieval_context_sha256=item["retrieval_context_sha256"],
            failure_signature=RETRIEVAL_EXCERPT_HYDRATION_FAILURE,
        )
        succeeded = await self._execute_runtime_model_repair_task(
            state,
            task,
            repair_task_id=authorization.output_repair_task_id,
            retrieval_task_id=item["retrieval_task_id"],
            runtime_model=item["runtime_model_id"],
            progress_callback=progress_callback,
            index=1,
            total=1,
        )
        if not succeeded:
            return await self._fail(
                state,
                f"Runtime output repair failed for {task.task_id}; additional repair is blocked",
                progress_callback,
            )

        # Existing Cycle 032 remains authoritative for every untouched task,
        # integration and Quality Review.  The successful result is durable
        # before any next authorization or paid invocation.
        return await self.recover_runtime_model_drift(
            workflow_id,
            capability_client=capability_client,
            progress_callback=progress_callback,
        )

    async def recover_runtime_adapter_contract(
        self,
        workflow_id: str,
        *,
        capability_client: OpenRouterModelCapabilityClient | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        """Repair one source-identity adapter failure, then resume untouched tasks."""

        state = self.repository.load(workflow_id)
        if state.status not in {
            WorkflowStatus.FAILED.value,
            WorkflowStatus.PARTIALLY_COMPLETED.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError(
                "Runtime adapter repair requires a failed or incomplete Researcher workflow"
            )
        if state.research_report is not None:
            raise ValueError("Runtime adapter repair is only for pre-integration failures")
        restored = self._restore_results_from_message_history(state, persist=False)
        if restored:
            self.repository.save(state)
        audit = self.inspect_runtime_adapter_repair(workflow_id)
        candidates = [item for item in audit["tasks"] if item["eligible"]]
        if len(candidates) != 1:
            details = ", ".join(
                f"{item['task_id']}:{'/'.join(item['blockers'])}"
                for item in audit["tasks"]
                if item["blockers"]
            )
            raise ValueError(
                "RUNTIME_ADAPTER_REPAIR_BLOCKED: exactly one eligible source-identity "
                f"failure is required; found {len(candidates)}. {details}"
            )
        item = candidates[0]
        task = next(
            ResearchTask.model_validate(raw_task)
            for raw_task in state.research_tasks
            if raw_task.get("task_id") == item["task_id"]
        )
        provider_id = getattr(self.registry.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Runtime adapter repair requires a stable provider ID")
        authorization = self.runtime_adapter_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=task.target_agent_id,
            original_task_id=task.task_id,
            source_error_message_id=item["source_error_message_id"],
            source_error_class=item["source_error_class"],
            model_id=item["runtime_model_id"],
            retrieval_id=item["retrieval_id"],
            retrieval_context_sha256=item["retrieval_context_sha256"],
            failure_signature=SOURCE_IDENTITY_CANONICALIZATION_FAILURE,
        )
        succeeded = await self._execute_runtime_model_repair_task(
            state,
            task,
            repair_task_id=authorization.repair_task_id,
            retrieval_task_id=item["retrieval_task_id"],
            runtime_model=item["runtime_model_id"],
            progress_callback=progress_callback,
            index=1,
            total=1,
        )
        if not succeeded:
            return await self._fail(
                state,
                f"Runtime adapter repair failed for {task.task_id}; additional repair is blocked",
                progress_callback,
            )
        return await self.recover_runtime_model_drift(
            workflow_id,
            capability_client=capability_client,
            progress_callback=progress_callback,
        )

    async def recover_runtime_identity_contract(
        self,
        workflow_id: str,
        *,
        capability_client: OpenRouterModelCapabilityClient | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        """Repair one redundant composite identity, then resume untouched tasks."""

        state = self.repository.load(workflow_id)
        if state.status not in {
            WorkflowStatus.FAILED.value,
            WorkflowStatus.PARTIALLY_COMPLETED.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError(
                "Runtime identity repair requires a failed or incomplete Researcher workflow"
            )
        if state.research_report is not None:
            raise ValueError("Runtime identity repair is only for pre-integration failures")
        restored = self._restore_results_from_message_history(state, persist=False)
        if restored:
            self.repository.save(state)
        audit = self.inspect_runtime_identity_repair(workflow_id)
        candidates = [item for item in audit["tasks"] if item["eligible"]]
        if len(candidates) != 1:
            details = ", ".join(
                f"{item['task_id']}:{'/'.join(item['blockers'])}"
                for item in audit["tasks"]
                if item["blockers"]
            )
            raise ValueError(
                "RUNTIME_IDENTITY_REPAIR_BLOCKED: exactly one eligible composite-identity "
                f"failure is required; found {len(candidates)}. {details}"
            )
        item = candidates[0]
        task = next(
            ResearchTask.model_validate(raw_task)
            for raw_task in state.research_tasks
            if raw_task.get("task_id") == item["task_id"]
        )
        provider_id = getattr(self.registry.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Runtime identity repair requires a stable provider ID")
        authorization = self.runtime_identity_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=task.target_agent_id,
            original_task_id=task.task_id,
            source_error_message_id=item["source_error_message_id"],
            source_error_class=item["source_error_class"],
            model_id=item["runtime_model_id"],
            retrieval_id=item["retrieval_id"],
            retrieval_context_sha256=item["retrieval_context_sha256"],
            failure_signature=SOURCE_REDUNDANT_IDENTITY_HYDRATION_FAILURE,
        )
        succeeded = await self._execute_runtime_model_repair_task(
            state,
            task,
            repair_task_id=authorization.repair_task_id,
            retrieval_task_id=item["retrieval_task_id"],
            runtime_model=item["runtime_model_id"],
            progress_callback=progress_callback,
            index=1,
            total=1,
        )
        if not succeeded:
            return await self._fail(
                state,
                f"Runtime identity repair failed for {task.task_id}; additional repair is blocked",
                progress_callback,
            )
        return await self.recover_runtime_model_drift(
            workflow_id,
            capability_client=capability_client,
            progress_callback=progress_callback,
        )

    async def recover_runtime_provenance_contract(
        self,
        workflow_id: str,
        *,
        capability_client: OpenRouterModelCapabilityClient | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        """Repair one provenance ownership failure, then resume untouched tasks."""

        state = self.repository.load(workflow_id)
        if state.status not in {
            WorkflowStatus.FAILED.value,
            WorkflowStatus.PARTIALLY_COMPLETED.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError(
                "Runtime provenance repair requires a failed or incomplete Researcher workflow"
            )
        if state.research_report is not None:
            raise ValueError("Runtime provenance repair is only for pre-integration failures")
        restored = self._restore_results_from_message_history(state, persist=False)
        if restored:
            self.repository.save(state)
        audit = self.inspect_runtime_provenance_repair(workflow_id)
        candidates = [item for item in audit["tasks"] if item["eligible"]]
        if len(candidates) != 1:
            details = ", ".join(
                f"{item['task_id']}:{'/'.join(item['blockers'])}"
                for item in audit["tasks"]
                if item["blockers"]
            )
            raise ValueError(
                "RUNTIME_PROVENANCE_REPAIR_BLOCKED: exactly one eligible provenance "
                f"failure is required; found {len(candidates)}. {details}"
            )
        item = candidates[0]
        task = next(
            ResearchTask.model_validate(raw_task)
            for raw_task in state.research_tasks
            if raw_task.get("task_id") == item["task_id"]
        )
        provider_id = getattr(self.registry.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Runtime provenance repair requires a stable provider ID")
        authorization = self.runtime_provenance_repair_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=task.target_agent_id,
            original_task_id=task.task_id,
            source_error_message_id=item["source_error_message_id"],
            source_error_class=item["source_error_class"],
            model_id=item["runtime_model_id"],
            retrieval_id=item["retrieval_id"],
            retrieval_context_sha256=item["retrieval_context_sha256"],
            failure_signature=SOURCE_PROVENANCE_OWNERSHIP_FAILURE,
        )
        succeeded = await self._execute_runtime_model_repair_task(
            state,
            task,
            repair_task_id=authorization.repair_task_id,
            retrieval_task_id=item["retrieval_task_id"],
            runtime_model=item["runtime_model_id"],
            progress_callback=progress_callback,
            index=1,
            total=1,
        )
        if not succeeded:
            return await self._fail(
                state,
                f"Runtime provenance repair failed for {task.task_id}; additional repair is blocked",
                progress_callback,
            )
        return await self.recover_runtime_model_drift(
            workflow_id,
            capability_client=capability_client,
            progress_callback=progress_callback,
        )

    @staticmethod
    def _stable_human_evidence_id(prefix: str, *parts: object) -> str:
        digest = hashlib.sha256(
            "\x1f".join(str(part) for part in parts).encode("utf-8")
        ).hexdigest()[:24]
        return f"{prefix}_{digest}"

    @staticmethod
    def _canonical_sha256(payload: Any) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _integrity_repair_hash_payload(repair: Any) -> dict[str, Any]:
        data = repair.model_dump(mode="json")
        if isinstance(repair, ResearchReportIntegrityRepair):
            # The before/after identity fields describe this canonical payload;
            # excluding them avoids a circular hash while legacy repair hashes
            # retain their byte-for-byte input shape.
            data.pop("evidence_set_sha256_before", None)
            data.pop("evidence_set_sha256_after", None)
        return data

    def _human_evidence_set_sha256(
        self,
        report: ResearchReport,
        *,
        quality_review_id: str,
        review: ResearchQualityReviewOutput,
        repairs: list[Any],
    ) -> str:
        return self._canonical_sha256(
            {
                "research_report": report.model_dump(mode="json"),
                "quality_review_id": quality_review_id,
                "quality_review": self._review_summary(review),
                "integrity_repairs": [
                    self._integrity_repair_hash_payload(item)
                    for item in repairs
                    if item.quality_review_id == quality_review_id
                ],
            }
        )

    @staticmethod
    def _review_summary(review: ResearchQualityReviewOutput) -> dict[str, Any]:
        return review.model_dump(mode="json", exclude={"approved_research_report"})

    @staticmethod
    def _saved_quality_review(
        state: ResearcherWorkflowState,
    ) -> ResearchQualityReviewOutput:
        if state.review_result is None:
            raise ValueError("Researcher workflow has no saved Quality Review")
        payload = dict(state.review_result)
        if payload.get("status") in {"approved", "approved_with_conditions"}:
            if state.research_report is None:
                raise ValueError("Approved Quality Review has no saved Research Report")
            payload["approved_research_report"] = state.research_report
        return ResearchQualityReviewOutput.model_validate(payload)

    def _latest_quality_review_message(
        self,
        state: ResearcherWorkflowState,
    ) -> PMPMessage:
        if state.review_result is None:
            raise ValueError("Researcher workflow has no saved Quality Review")
        expected = self._review_summary(self._saved_quality_review(state))
        for message in reversed(state.message_history):
            if (
                message.sender_agent_id != QUALITY_REVIEWER_ID
                or message.receiver_agent_id != self.agent_id
                or message.message_type != MessageType.REVIEW.value
            ):
                continue
            try:
                candidate = ResearchQualityReviewOutput.model_validate(message.payload)
            except Exception:
                continue
            if self._review_summary(candidate) == expected:
                return message
        raise ValueError("Saved Quality Review is not correlated to a PMP review message")

    @staticmethod
    def _classify_review_finding(finding: Any) -> ResearchFindingType:
        explicit = ResearchFindingType(finding.finding_type)
        if explicit != ResearchFindingType.UNCLASSIFIED:
            return explicit
        text = f"{finding.issue}\n{finding.required_action}".casefold()
        if any(marker.casefold() in text for marker in HARD_INTEGRITY_MARKERS):
            return ResearchFindingType.HARD_INTEGRITY_FAILURE
        if any(marker.casefold() in text for marker in EVIDENCE_SUFFICIENCY_MARKERS):
            return ResearchFindingType.EVIDENCE_SUFFICIENCY
        return ResearchFindingType.UNCLASSIFIED

    @staticmethod
    def _finding_source_id(finding: Any) -> str | None:
        match = SOURCE_ID_PATTERN.search(f"{finding.issue}\n{finding.required_action}")
        return match.group(0) if match else None

    @staticmethod
    def _is_closed_official_host(host: str) -> bool:
        normalized = host.casefold().rstrip(".")
        return any(
            normalized == allowed or normalized.endswith(f".{allowed}")
            for allowed in OFFICIAL_INDUSTRY_REPAIR_HOSTS
        )

    @staticmethod
    def _official_industry_repair_is_applicable(
        report: ResearchReport,
        finding: Any,
    ) -> bool:
        source_id = ResearcherManager._finding_source_id(finding)
        if source_id is None:
            return False
        source = next((item for item in report.sources if item.source_id == source_id), None)
        if source is None or source.source_type != ResearchSourceType.INDUSTRY.value:
            return False
        host = (urlsplit(str(source.url)).hostname or "").casefold()
        return ResearcherManager._is_closed_official_host(host)

    @staticmethod
    def _dedupe_report_limitations(
        limitations: list[str],
        sources: list[ResearchSource],
    ) -> list[str]:
        """Remove exact report-level repeats without rewriting any text.

        Source-level limitations remain on their source.  The top-level Report
        keeps only limitations which are not an exact Unicode duplicate of a
        source disclosure, and the first occurrence of every remaining string.
        """

        source_limitations = {
            limitation
            for source in sources
            for limitation in source.limitations
        }
        seen: set[str] = set()
        result: list[str] = []
        for limitation in limitations:
            if limitation in source_limitations or limitation in seen:
                continue
            seen.add(limitation)
            result.append(limitation)
        return result

    @staticmethod
    def _recognized_news_profile(url: object) -> dict[str, str] | None:
        host = (urlsplit(str(url)).hostname or "").casefold().rstrip(".")
        for recognized_host, profile in RECOGNIZED_NEWS_MEDIA_BY_HOST.items():
            if host == recognized_host or host.endswith("." + recognized_host):
                return dict(profile)
        return None

    @staticmethod
    def _expert_identity_is_absent(source: ResearchSource) -> bool:
        if source.source_type != ResearchSourceType.EXPERT.value:
            return False
        return not any(
            source.source_specific_metadata.get(field_name)
            for field_name in ("expert_name", "affiliation")
        )

    @classmethod
    def _canonicalize_recognized_media_source(
        cls,
        source: ResearchSource,
    ) -> tuple[ResearchSource, bool]:
        """Narrowly reclassify a stored media article without changing identity."""

        if not cls._expert_identity_is_absent(source):
            return source, False
        metadata = dict(source.source_specific_metadata)
        profile = cls._recognized_news_profile(source.url)
        context = str(metadata.get("statement_context") or "").casefold()
        if profile is None or not any(
            marker.casefold() in context for marker in RECOGNIZED_NEWS_CONTEXT_MARKERS
        ):
            return source, False

        before = source.model_dump(mode="json")
        merged_evidence_ids = list(metadata.get("merged_evidence_ids") or [])
        news_metadata: dict[str, Any] = dict(profile)
        if merged_evidence_ids:
            news_metadata["merged_evidence_ids"] = merged_evidence_ids
        repaired_data = dict(before)
        repaired_data["source_type"] = ResearchSourceType.NEWS.value
        repaired_data["source_specific_metadata"] = news_metadata
        repaired = ResearchSource.model_validate(repaired_data)

        immutable_fields = (
            "source_id",
            "evidence_id",
            "research_question_ids",
            "title",
            "source_name",
            "url",
            "author_or_organization",
            "published_at",
            "retrieved_at",
            "summary",
            "relevant_excerpt",
            "stance",
            "reliability",
            "directness",
            "primary_source",
            "geographic_scope",
            "time_scope",
            "limitations",
        )
        after = repaired.model_dump(mode="json")
        if any(before[field] != after[field] for field in immutable_fields):
            raise ValueError("Media classification repair changed immutable source identity")
        return repaired, True

    @classmethod
    def _limitation_dedupe_repair_is_applicable(
        cls,
        report: ResearchReport,
        finding: Any,
    ) -> bool:
        text = f"{finding.issue}\n{finding.required_action}".casefold()
        if (
            finding.target_agent_id != "researcher.manager"
            or "research_limitations" not in text
        ):
            return False
        return cls._dedupe_report_limitations(
            list(report.research_limitations),
            report.sources,
        ) != list(report.research_limitations)

    @classmethod
    def _recognized_media_repair_is_applicable(
        cls,
        report: ResearchReport,
        finding: Any,
    ) -> bool:
        source_id = cls._finding_source_id(finding)
        if source_id is None:
            return False
        source = next((item for item in report.sources if item.source_id == source_id), None)
        if source is None:
            return False
        _repaired, changed = cls._canonicalize_recognized_media_source(source)
        return changed

    @staticmethod
    def _duplicate_tracking_repair_plan(
        report: ResearchReport,
        finding: Any,
    ) -> DuplicateTrackingPlan | None:
        return plan_duplicate_tracking_repair(report, finding)

    def _recompute_report_coverage_data(
        self,
        state: ResearcherWorkflowState,
        data: dict[str, Any],
    ) -> None:
        categories = {item.value: [] for item in ResearchSourceType}
        for source in data["sources"]:
            categories[source["source_type"]].append(source["source_id"])
        data["source_perspectives"] = categories
        data["sources_by_category"] = categories
        data["source_count_by_category"] = {
            category: len(source_ids) for category, source_ids in categories.items()
        }

        existing_gap_by_key = {
            (item["research_question_id"], item["missing_category"]): item
            for item in data["evidence_gaps"]
        }
        repaired_gaps: list[dict[str, Any]] = []
        missing_lines: list[str] = []
        for coverage in data["research_questions"]:
            related = [
                source
                for source in data["sources"]
                if coverage["research_question_id"]
                in source["research_question_ids"]
            ]
            completed = list(
                dict.fromkeys(source["source_type"] for source in related)
            )
            missing = [
                category
                for category in coverage["required_categories"]
                if category not in completed
            ]
            coverage["completed_categories"] = completed
            coverage["evidence_ids"] = [
                source["evidence_id"] for source in related
            ]
            coverage["coverage_status"] = (
                CoverageStatus.COMPLETE.value
                if not missing
                else CoverageStatus.PARTIAL.value
                if completed
                else CoverageStatus.NO_RESULT.value
            )
            for category in missing:
                key = (coverage["research_question_id"], category)
                repaired_gaps.append(
                    existing_gap_by_key.get(key)
                    or {
                        "gap_id": self._stable_human_evidence_id(
                            "gap",
                            state.workflow_id,
                            coverage["research_question_id"],
                            category,
                        ),
                        "research_question_id": coverage["research_question_id"],
                        "missing_category": category,
                        "description": (
                            f"{category} evidence is missing for this Research Question"
                        ),
                    }
                )
            if missing:
                missing_lines.append(
                    f"{coverage['research_question_id']}: missing "
                    + ", ".join(missing)
                    + " evidence"
                )
        data["evidence_gaps"] = repaired_gaps
        data["unresolved_questions"] = list(
            dict.fromkeys(
                [
                    item
                    for item in data["unresolved_questions"]
                    if ": missing " not in item
                ]
                + missing_lines
            )
        )

    def _repair_report_limitation_duplicates(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        *,
        quality_review_id: str,
        review: ResearchQualityReviewOutput,
        finding: Any,
    ) -> ResearchReport:
        existing = next(
            (
                item
                for item in state.human_evidence_integrity_repairs
                if item.quality_review_id == quality_review_id
                and item.finding_id == finding.finding_id
            ),
            None,
        )
        if existing is not None:
            return report

        original = list(report.research_limitations)
        repaired_limitations = self._dedupe_report_limitations(
            original,
            report.sources,
        )
        if repaired_limitations == original:
            raise ValueError("No exact Report limitation duplicate is repairable")
        kept_counts: dict[str, int] = {}
        for item in repaired_limitations:
            kept_counts[item] = kept_counts.get(item, 0) + 1
        removed: list[str] = []
        for item in original:
            remaining = kept_counts.get(item, 0)
            if remaining:
                kept_counts[item] = remaining - 1
            else:
                removed.append(item)

        before_report_sha256 = self._canonical_sha256(report.model_dump(mode="json"))
        before_evidence_sha256 = self._human_evidence_set_sha256(
            report,
            quality_review_id=quality_review_id,
            review=review,
            repairs=state.human_evidence_integrity_repairs,
        )
        data = report.model_dump(mode="json")
        data["research_limitations"] = repaired_limitations
        repaired = ResearchReport.model_validate(data)
        after_report_sha256 = self._canonical_sha256(repaired.model_dump(mode="json"))
        repair_id = self._stable_human_evidence_id(
            "integrity_repair",
            state.workflow_id,
            quality_review_id,
            finding.finding_id,
        )
        applied_at = utc_now()
        repair_fields = {
            "repair_id": repair_id,
            "workflow_id": state.workflow_id,
            "quality_review_id": quality_review_id,
            "finding_id": finding.finding_id,
            "repair_kind": "report_limitation_exact_deduplication",
            "removed_report_limitation_count": len(removed),
            "removed_report_limitations": list(dict.fromkeys(removed)),
            "report_sha256_before": before_report_sha256,
            "report_sha256_after": after_report_sha256,
            "evidence_set_sha256_before": before_evidence_sha256,
            "provider_calls": 0,
            "retrieval_calls": 0,
            "rationale": (
                "Removed only exact Unicode duplicates from the Report-level "
                "research_limitations list; source-level disclosures and text are unchanged."
            ),
            "applied_at": applied_at,
        }
        provisional = ResearchReportIntegrityRepair(
            **repair_fields,
            evidence_set_sha256_after="0" * 64,
        )
        after_evidence_sha256 = self._human_evidence_set_sha256(
            repaired,
            quality_review_id=quality_review_id,
            review=review,
            repairs=state.human_evidence_integrity_repairs + [provisional],
        )
        repair = ResearchReportIntegrityRepair(
            **repair_fields,
            evidence_set_sha256_after=after_evidence_sha256,
        )
        state.human_evidence_integrity_repairs.append(repair)
        state.research_report = repaired.model_dump(mode="json")
        state.collected_sources = [
            source.model_dump(mode="json") for source in repaired.sources
        ]
        self.repository.save_report(repaired)
        self.repository.save(state)
        return repaired

    def _repair_recognized_media_source(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        *,
        quality_review_id: str,
        review: ResearchQualityReviewOutput,
        finding: Any,
    ) -> ResearchReport:
        source_id = self._finding_source_id(finding)
        if source_id is None:
            raise ValueError("Integrity finding does not identify a source_id")
        existing = next(
            (
                item
                for item in state.human_evidence_integrity_repairs
                if item.quality_review_id == quality_review_id
                and item.finding_id == finding.finding_id
            ),
            None,
        )
        if existing is not None:
            return report

        original_source = next(
            (item for item in report.sources if item.source_id == source_id),
            None,
        )
        if original_source is None:
            raise ValueError(f"Integrity repair source is absent: {source_id}")
        repaired_source, changed = self._canonicalize_recognized_media_source(
            original_source
        )
        if not changed:
            raise ValueError("Source cannot be deterministically reclassified as NEWS")

        before_report_sha256 = self._canonical_sha256(report.model_dump(mode="json"))
        before_evidence_sha256 = self._human_evidence_set_sha256(
            report,
            quality_review_id=quality_review_id,
            review=review,
            repairs=state.human_evidence_integrity_repairs,
        )
        data = report.model_dump(mode="json")
        raw_source = next(
            item for item in data["sources"] if item["source_id"] == source_id
        )
        raw_source.clear()
        raw_source.update(repaired_source.model_dump(mode="json"))
        raw_metadata = next(
            (item for item in data["source_metadata"] if item["source_id"] == source_id),
            None,
        )
        if raw_metadata is None:
            raise ValueError(f"Integrity repair source_metadata is absent: {source_id}")
        raw_metadata["source_type"] = ResearchSourceType.NEWS.value
        raw_metadata["source_specific_metadata"] = dict(
            repaired_source.source_specific_metadata
        )
        self._recompute_report_coverage_data(state, data)
        repaired = ResearchReport.model_validate(data)
        if len(repaired.sources) != len(report.sources):
            raise ValueError("Media repair changed the Research Report source count")
        after_report_sha256 = self._canonical_sha256(repaired.model_dump(mode="json"))
        repair_id = self._stable_human_evidence_id(
            "integrity_repair",
            state.workflow_id,
            quality_review_id,
            finding.finding_id,
        )
        applied_at = utc_now()
        repair_fields = {
            "repair_id": repair_id,
            "workflow_id": state.workflow_id,
            "quality_review_id": quality_review_id,
            "finding_id": finding.finding_id,
            "repair_kind": "recognized_media_source_reclassification",
            "source_id": source_id,
            "previous_source_type": ResearchSourceType.EXPERT.value,
            "repaired_source_type": ResearchSourceType.NEWS.value,
            "report_sha256_before": before_report_sha256,
            "report_sha256_after": after_report_sha256,
            "evidence_set_sha256_before": before_evidence_sha256,
            "provider_calls": 0,
            "retrieval_calls": 0,
            "rationale": (
                "The immutable xtech.nikkei.com URL and saved reporting context identify "
                "a recognized media article, while both EXPERT identity fields are absent."
            ),
            "applied_at": applied_at,
        }
        provisional = ResearchReportIntegrityRepair(
            **repair_fields,
            evidence_set_sha256_after="0" * 64,
        )
        after_evidence_sha256 = self._human_evidence_set_sha256(
            repaired,
            quality_review_id=quality_review_id,
            review=review,
            repairs=state.human_evidence_integrity_repairs + [provisional],
        )
        repair = ResearchReportIntegrityRepair(
            **repair_fields,
            evidence_set_sha256_after=after_evidence_sha256,
        )
        state.human_evidence_integrity_repairs.append(repair)
        state.research_report = repaired.model_dump(mode="json")
        state.collected_sources = [
            source.model_dump(mode="json") for source in repaired.sources
        ]
        self.repository.save_report(repaired)
        self.repository.save(state)
        return repaired

    def _repair_official_industry_source(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        *,
        quality_review_id: str,
        finding: Any,
    ) -> ResearchReport:
        source_id = self._finding_source_id(finding)
        if source_id is None:
            raise ValueError("Integrity finding does not identify a source_id")
        existing = next(
            (
                item
                for item in state.human_evidence_integrity_repairs
                if item.quality_review_id == quality_review_id
                and item.finding_id == finding.finding_id
            ),
            None,
        )
        if existing is not None:
            return report

        data = report.model_dump(mode="json")
        raw_source = next(
            (item for item in data["sources"] if item["source_id"] == source_id),
            None,
        )
        if raw_source is None:
            raise ValueError(f"Integrity repair source is absent: {source_id}")
        host = (urlsplit(str(raw_source["url"])).hostname or "").casefold()
        if (
            raw_source["source_type"] != ResearchSourceType.INDUSTRY.value
            or not self._is_closed_official_host(host)
        ):
            raise ValueError("Source cannot be deterministically reclassified as GOVERNMENT")

        organization = canonical_source_label(
            raw_source["url"],
            raw_source["title"],
            source_type=ResearchSourceType.GOVERNMENT,
        )
        previous_metadata = dict(raw_source.get("source_specific_metadata") or {})
        merged_evidence_ids = list(previous_metadata.get("merged_evidence_ids") or [])
        government_metadata: dict[str, Any] = {
            "organization": organization,
            "country": canonical_country_from_url(raw_source["url"]),
            "document_type": "official_document",
        }
        if merged_evidence_ids:
            government_metadata["merged_evidence_ids"] = merged_evidence_ids
        raw_source["source_type"] = ResearchSourceType.GOVERNMENT.value
        raw_source["source_name"] = organization
        raw_source["author_or_organization"] = organization
        raw_source["source_specific_metadata"] = government_metadata

        raw_metadata = next(
            (item for item in data["source_metadata"] if item["source_id"] == source_id),
            None,
        )
        if raw_metadata is None:
            raise ValueError(f"Integrity repair source_metadata is absent: {source_id}")
        raw_metadata["source_type"] = ResearchSourceType.GOVERNMENT.value
        raw_metadata["source_name"] = organization
        raw_metadata["author_or_organization"] = organization
        raw_metadata["source_specific_metadata"] = government_metadata

        source_by_id = {item["source_id"]: item for item in data["sources"]}
        categories = {item.value: [] for item in ResearchSourceType}
        for item in data["sources"]:
            categories[item["source_type"]].append(item["source_id"])
        data["source_perspectives"] = categories
        data["sources_by_category"] = categories
        data["source_count_by_category"] = {
            category: len(source_ids) for category, source_ids in categories.items()
        }

        for coverage in data["research_questions"]:
            related = [
                source_by_id[item.source_id]
                for item in report.evidence_items
                if coverage["research_question_id"] in item.research_question_ids
            ]
            completed = list(
                dict.fromkeys(item["source_type"] for item in related)
            )
            missing = [
                category
                for category in coverage["required_categories"]
                if category not in completed
            ]
            coverage["completed_categories"] = completed
            coverage["coverage_status"] = (
                CoverageStatus.COMPLETE.value
                if not missing
                else CoverageStatus.PARTIAL.value
                if completed
                else CoverageStatus.NO_RESULT.value
            )

        existing_gap_by_key = {
            (item["research_question_id"], item["missing_category"]): item
            for item in data["evidence_gaps"]
        }
        repaired_gaps: list[dict[str, Any]] = []
        missing_lines: list[str] = []
        for coverage in data["research_questions"]:
            missing = [
                category
                for category in coverage["required_categories"]
                if category not in coverage["completed_categories"]
            ]
            for category in missing:
                key = (coverage["research_question_id"], category)
                repaired_gaps.append(
                    existing_gap_by_key.get(key)
                    or {
                        "gap_id": self._stable_human_evidence_id(
                            "gap",
                            state.workflow_id,
                            coverage["research_question_id"],
                            category,
                        ),
                        "research_question_id": coverage["research_question_id"],
                        "missing_category": category,
                        "description": (
                            f"{category} evidence is missing for this Research Question"
                        ),
                    }
                )
            if missing:
                missing_lines.append(
                    f"{coverage['research_question_id']}: missing "
                    + ", ".join(missing)
                    + " evidence"
                )
        data["evidence_gaps"] = repaired_gaps
        data["unresolved_questions"] = list(
            dict.fromkeys(
                [
                    item
                    for item in data["unresolved_questions"]
                    if ": missing " not in item
                ]
                + missing_lines
            )
        )

        repair_id = self._stable_human_evidence_id(
            "integrity_repair", state.workflow_id, quality_review_id, finding.finding_id
        )
        limitation = (
            f"{repair_id}: {source_id} was deterministically reclassified from "
            "INDUSTRY to GOVERNMENT because its publisher is a closed-allowlist "
            f"official host ({host}); this does not supply missing INDUSTRY evidence."
        )
        data["research_limitations"] = list(
            dict.fromkeys(data["research_limitations"] + [limitation])
        )
        repaired = ResearchReport.model_validate(data)
        repair = HumanEvidenceSourceReclassificationRepair(
            repair_id=repair_id,
            workflow_id=state.workflow_id,
            quality_review_id=quality_review_id,
            finding_id=finding.finding_id,
            repair_kind="official_industry_source_reclassification",
            source_id=source_id,
            previous_source_type=ResearchSourceType.INDUSTRY.value,
            repaired_source_type=ResearchSourceType.GOVERNMENT.value,
            rationale=(
                "Closed official-host classification overrides an INDUSTRY publisher "
                "classification; original Quality Review remains unchanged."
            ),
        )
        state.human_evidence_integrity_repairs.append(repair)
        state.research_report = repaired.model_dump(mode="json")
        state.collected_sources = [
            source.model_dump(mode="json") for source in repaired.sources
        ]
        self.repository.save_report(repaired)
        self.repository.save(state)
        return repaired

    def _repair_duplicate_document_tracking(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        *,
        quality_review_id: str,
        finding: Any,
        plan: DuplicateTrackingPlan,
    ) -> ResearchReport:
        existing = next(
            (
                item
                for item in state.human_evidence_integrity_repairs
                if item.quality_review_id == quality_review_id
                and item.finding_id == finding.finding_id
            ),
            None,
        )
        if existing is not None:
            if not isinstance(existing, ResearchSourceDuplicateTrackingRepair):
                raise ValueError(
                    "DUPLICATE_TRACKING_CONFLICT: finding already has another repair kind"
                )
            self.repository.create_human_evidence_integrity_repair_once(existing)
            return report

        family_source_ids = {plan.canonical_source_id, *plan.related_source_ids}
        report_sha256_before = self._canonical_sha256(report.model_dump(mode="json"))
        relation_sha256_before = relation_metadata_sha256(report, family_source_ids)
        immutable_sha256_before = immutable_report_sha256(report)
        repaired = apply_duplicate_tracking_plan(report, plan)
        report_sha256_after = self._canonical_sha256(repaired.model_dump(mode="json"))
        relation_sha256_after = relation_metadata_sha256(repaired, family_source_ids)
        immutable_sha256_after = immutable_report_sha256(repaired)

        repair = ResearchSourceDuplicateTrackingRepair(
            repair_id=self._stable_human_evidence_id(
                "integrity_repair",
                state.workflow_id,
                quality_review_id,
                finding.finding_id,
            ),
            workflow_id=state.workflow_id,
            quality_review_id=quality_review_id,
            finding_id=finding.finding_id,
            repair_kind="research_source_duplicate_tracking",
            document_family_id=plan.document_family_id,
            canonical_source_id=plan.canonical_source_id,
            canonical_evidence_id=plan.canonical_evidence_id,
            related_source_ids=list(plan.related_source_ids),
            merged_evidence_ids=list(plan.merged_evidence_ids),
            report_sha256_before=report_sha256_before,
            report_sha256_after=report_sha256_after,
            relation_metadata_sha256_before=relation_sha256_before,
            relation_metadata_sha256_after=relation_sha256_after,
            immutable_content_sha256_before=immutable_sha256_before,
            immutable_content_sha256_after=immutable_sha256_after,
            provider_calls=0,
            retrieval_calls=0,
            rationale=(
                "Preserved every Source, Evidence, URL, Research Question and content field; "
                "added only the deterministic same-document-family merged Evidence relation."
            ),
        )
        state.human_evidence_integrity_repairs.append(repair)
        state.research_report = repaired.model_dump(mode="json")
        state.collected_sources = [
            source.model_dump(mode="json") for source in repaired.sources
        ]
        self.repository.save_report(repaired)
        self.repository.save(state)
        self.repository.create_human_evidence_integrity_repair_once(repair)
        return repaired

    def _gate_summary(
        self,
        state: ResearcherWorkflowState,
        *,
        apply_repairs: bool,
        apply_duplicate_tracking_repair: bool = False,
    ) -> HumanEvidenceGateSummary:
        if state.research_report is None or state.review_result is None:
            raise ValueError("Human Evidence Gate requires a report and Quality Review")
        technical_error = json.dumps(
            state.error or {}, ensure_ascii=False, sort_keys=True
        ).casefold()
        technical_markers = (
            "schema",
            "pmp",
            "provenance",
            "malformed",
            "source integrity",
            "provider failure",
        )
        handoff_recovery = (
            state.human_evidence_decision is not None
            and (state.error or {}).get("code")
            == "HUMAN_EVIDENCE_HANDOFF_INCOMPLETE"
        )
        if not handoff_recovery and (
            (
                state.status == WorkflowStatus.FAILED.value
                and state.human_evidence_decision is None
            )
            or any(marker in technical_error for marker in technical_markers)
        ):
            raise ValueError(
                "Human Evidence Gate is closed by an unresolved technical integrity failure"
            )
        report = ResearchReport.model_validate(state.research_report)
        review = self._saved_quality_review(state)
        review_message = self._latest_quality_review_message(state)
        quality_review_id = review_message.message_id

        if apply_repairs:
            for finding in review.findings:
                if self._classify_review_finding(
                    finding
                ) != ResearchFindingType.HARD_INTEGRITY_FAILURE:
                    continue
                if is_duplicate_tracking_finding(finding):
                    if not apply_duplicate_tracking_repair:
                        continue
                    duplicate_plan = self._duplicate_tracking_repair_plan(
                        report, finding
                    )
                    if duplicate_plan is None:
                        raise ValueError(
                            "Duplicate tracking finding lost its repair classification"
                        )
                    report = self._repair_duplicate_document_tracking(
                        state,
                        report,
                        quality_review_id=quality_review_id,
                        finding=finding,
                        plan=duplicate_plan,
                    )
                elif self._limitation_dedupe_repair_is_applicable(report, finding):
                    report = self._repair_report_limitation_duplicates(
                        state,
                        report,
                        quality_review_id=quality_review_id,
                        review=review,
                        finding=finding,
                    )
                elif self._official_industry_repair_is_applicable(report, finding):
                    report = self._repair_official_industry_source(
                        state,
                        report,
                        quality_review_id=quality_review_id,
                        finding=finding,
                    )
                elif self._recognized_media_repair_is_applicable(report, finding):
                    report = self._repair_recognized_media_source(
                        state,
                        report,
                        quality_review_id=quality_review_id,
                        review=review,
                        finding=finding,
                    )

        repair_by_finding_id = {
            item.finding_id: item
            for item in state.human_evidence_integrity_repairs
            if item.quality_review_id == quality_review_id
        }
        coverage_by_question_id = {
            item.research_question_id: item for item in report.research_questions
        }
        classification_repair_source_ids = {
            item.source_id
            for item in state.human_evidence_integrity_repairs
            if item.quality_review_id == quality_review_id
            and item.repair_kind in {
                "official_industry_source_reclassification",
                "recognized_media_source_reclassification",
            }
        }

        def coverage_recomputation_resolves(finding: Any) -> bool:
            if not classification_repair_source_ids or not finding.research_question_id:
                return False
            try:
                required_category = self._source_type_for_agent(
                    finding.target_agent_id
                )
            except (TypeError, ValueError):
                return False
            coverage = coverage_by_question_id.get(finding.research_question_id)
            if (
                coverage is None
                or required_category not in coverage.required_categories
                or required_category not in coverage.completed_categories
            ):
                return False
            repaired_source_supplies_target = any(
                source.source_id in classification_repair_source_ids
                and source.source_type == required_category
                and finding.research_question_id in source.research_question_ids
                for source in report.sources
            )
            if not repaired_source_supplies_target:
                return False
            text = f"{finding.issue}\n{finding.required_action}".casefold()
            return any(
                marker in text for marker in ("missing", "0件", "未充足", "不足")
            )

        hard: list[EvidenceFindingDisposition] = []
        sufficiency: list[EvidenceFindingDisposition] = []
        resolved: list[EvidenceFindingDisposition] = []
        unclassified: list[EvidenceFindingDisposition] = []
        for finding in review.findings:
            finding_type = self._classify_review_finding(finding)
            repair = repair_by_finding_id.get(finding.finding_id)
            resolved_by_coverage = (
                finding_type == ResearchFindingType.EVIDENCE_SUFFICIENCY
                and coverage_recomputation_resolves(finding)
            )
            is_resolved = repair is not None or resolved_by_coverage
            if repair is not None:
                repair_kind = repair.repair_kind.replace("_", " ")
                resolution = f"Resolved by deterministic {repair_kind}"
            elif resolved_by_coverage:
                resolution = (
                    "Resolved by deterministic coverage recomputation after "
                    "source classification repair"
                )
            else:
                resolution = None
            disposition = EvidenceFindingDisposition(
                finding_id=finding.finding_id,
                finding_type=finding_type,
                severity=str(finding.severity),
                research_question_id=finding.research_question_id,
                target_agent_id=finding.target_agent_id,
                issue=finding.issue,
                required_action=finding.required_action,
                resolved=is_resolved,
                resolution=resolution,
            )
            if is_resolved:
                resolved.append(disposition)
            elif finding_type == ResearchFindingType.HARD_INTEGRITY_FAILURE:
                hard.append(disposition)
            elif finding_type == ResearchFindingType.EVIDENCE_SUFFICIENCY:
                sufficiency.append(disposition)
            else:
                unclassified.append(disposition)

        evidence_set_sha256 = self._human_evidence_set_sha256(
            report,
            quality_review_id=quality_review_id,
            review=review,
            repairs=state.human_evidence_integrity_repairs,
        )
        return HumanEvidenceGateSummary(
            workflow_id=state.workflow_id,
            research_report_id=report.research_report_id,
            quality_review_id=quality_review_id,
            evidence_set_sha256=evidence_set_sha256,
            source_count=len(report.sources),
            quality_review_status=str(review.status),
            recommended_action=(
                HumanEvidenceDecisionType.REVISE.value
                if review.status in {"revision_required", "blocked"}
                else HumanEvidenceDecisionType.ACCEPT.value
            ),
            hard_integrity_findings=hard,
            evidence_sufficiency_findings=sufficiency,
            resolved_integrity_findings=resolved,
            unclassified_findings=unclassified,
            eligible=(review.status != "blocked" and not hard and not unclassified),
            existing_decision=state.human_evidence_decision,
            revision_plan=state.evidence_revision_plan,
        )

    def inspect_human_evidence_gate(
        self,
        workflow_id: str,
    ) -> HumanEvidenceGateSummary:
        return self._gate_summary(self.repository.load(workflow_id), apply_repairs=False)

    def repair_human_evidence_integrity(
        self,
        workflow_id: str,
    ) -> ResearcherWorkflowState:
        """Run one local allowlisted integrity-repair pass without external calls."""

        state = self.repository.load(workflow_id)
        if state.human_evidence_decision is not None:
            raise ValueError(
                "Researcher integrity repair must run before the Human Evidence Decision"
            )
        if state.status not in {
            WorkflowStatus.BLOCKED.value,
            WorkflowStatus.WAITING_HUMAN_EVIDENCE_REVIEW.value,
        }:
            raise ValueError(
                "Researcher integrity repair requires BLOCKED or WAITING_HUMAN_EVIDENCE_REVIEW"
            )

        duplicate_repairs = [
            item
            for item in state.human_evidence_integrity_repairs
            if isinstance(item, ResearchSourceDuplicateTrackingRepair)
        ]
        current = self._gate_summary(state, apply_repairs=False)
        if current.eligible and duplicate_repairs:
            for repair in duplicate_repairs:
                self.repository.create_human_evidence_integrity_repair_once(repair)
            if state.status != WorkflowStatus.WAITING_HUMAN_EVIDENCE_REVIEW.value:
                self._prepare_human_evidence_gate(state)
            return state

        repair_count_before = len(state.human_evidence_integrity_repairs)
        summary = self._prepare_human_evidence_gate(
            state,
            apply_duplicate_tracking_repair=True,
        )
        if len(state.human_evidence_integrity_repairs) == repair_count_before:
            raise ValueError(
                "No allowlisted deterministic Researcher integrity finding is repairable"
            )
        if not summary.eligible:
            return state
        return state

    def _prepare_human_evidence_gate(
        self,
        state: ResearcherWorkflowState,
        *,
        apply_duplicate_tracking_repair: bool = False,
    ) -> HumanEvidenceGateSummary:
        summary = self._gate_summary(
            state,
            apply_repairs=True,
            apply_duplicate_tracking_repair=apply_duplicate_tracking_repair,
        )
        if not summary.eligible:
            state.status = WorkflowStatus.BLOCKED
            state.error = {
                "code": "HARD_INTEGRITY_FAILURE",
                "message": (
                    "Human Evidence Gate is closed because unresolved integrity or "
                    "unclassified findings remain"
                ),
            }
        else:
            state.status = WorkflowStatus.WAITING_HUMAN_EVIDENCE_REVIEW
            state.error = None
            state.completed_at = None
        state.current_agent_ids = []
        self.repository.save(state)
        return self._gate_summary(state, apply_repairs=False)

    def _decision_message(
        self,
        state: ResearcherWorkflowState,
        decision: HumanEvidenceDecision,
    ) -> PMPMessage:
        deterministic_message_id = str(
            uuid5(
                NAMESPACE_URL,
                f"prdcp:{state.workflow_id}:human-evidence:{decision.decision_id}",
            )
        )
        message = PMPMessage.create(
            workflow_id=state.workflow_id,
            parent_message_id=decision.quality_review_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id="deliberation.manager",
            message_type=MessageType.HUMAN_EVIDENCE_DECISION,
            objective="Record the Human Evidence Gate decision without changing AI findings",
            payload=decision.model_dump(mode="json"),
            constraints={
                "hard_integrity_override_allowed": False,
                "provider_calls_authorized": False,
                "accepted_gap_is_evidence": False,
            },
            context=PMPContext(
                current_stage="researcher.human_evidence_gate",
                previous_stage="researcher.quality_reviewer",
                next_stage=(
                    "researcher.evidence_revision_plan"
                    if decision.decision == HumanEvidenceDecisionType.REVISE.value
                    else "deliberation"
                ),
            ),
            metadata=PMPMetadata(
                status=(
                    MessageStatus.REVISION_REQUIRED
                    if decision.decision == HumanEvidenceDecisionType.REVISE.value
                    else MessageStatus.COMPLETED
                ),
                extensions={
                    "actor_type": decision.actor_type,
                    "actor_source": decision.actor_source,
                },
            ),
        )
        message.message_id = deterministic_message_id
        self.pmp_validator.validate(message)
        return message

    def _accepted_gaps(
        self,
        summary: HumanEvidenceGateSummary,
        decision: HumanEvidenceDecision,
    ) -> list[AcceptedEvidenceGap]:
        disposition_by_id = {
            item.finding_id: item for item in summary.evidence_sufficiency_findings
        }
        return [
            AcceptedEvidenceGap(
                finding_id=finding_id,
                quality_review_id=summary.quality_review_id,
                human_decision_id=decision.decision_id,
                research_question_id=disposition_by_id[finding_id].research_question_id,
                issue=disposition_by_id[finding_id].issue,
                required_action=disposition_by_id[finding_id].required_action,
            )
            for finding_id in decision.accepted_finding_ids
        ]

    def _revision_plan(
        self,
        state: ResearcherWorkflowState,
        summary: HumanEvidenceGateSummary,
        decision: HumanEvidenceDecision,
    ) -> EvidenceRevisionPlan:
        review = self._saved_quality_review(state)
        finding_ids = list(decision.revision_requested_finding_ids)
        finding_by_id = {item.finding_id: item for item in review.findings}
        target_agent_ids = list(
            dict.fromkeys(
                str(finding_by_id[finding_id].target_agent_id)
                for finding_id in finding_ids
                if finding_by_id[finding_id].target_agent_id
                and str(finding_by_id[finding_id].target_agent_id) != self.agent_id
            )
        )
        if not target_agent_ids:
            raise ValueError("Evidence revision has no executable specialist target")
        retrieval_calls = len(finding_ids)
        reasoning_calls = len(target_agent_ids)
        return EvidenceRevisionPlan(
            plan_id=self._stable_human_evidence_id(
                "evidence_revision_plan", state.workflow_id, summary.quality_review_id
            ),
            workflow_id=state.workflow_id,
            quality_review_id=summary.quality_review_id,
            human_decision_id=decision.decision_id,
            finding_ids=finding_ids,
            target_agent_ids=target_agent_ids,
            estimated_max_retrieval_calls=retrieval_calls,
            estimated_max_reasoning_calls=reasoning_calls,
            estimated_quality_review_calls=1,
            estimated_max_provider_calls=retrieval_calls + reasoning_calls + 1,
            provider_authorization_required=True,
        )

    def _plan_internal_revision_request(
        self,
        state: ResearcherWorkflowState,
        *,
        decision: HumanEvidenceDecision,
        plan: EvidenceRevisionPlan | None,
    ) -> RevisionRequestV1:
        if plan is None or state.research_report is None:
            raise ValueError("Researcher internal Revision requires a saved plan and report")
        review = self._saved_quality_review(state)
        finding_by_id = {item.finding_id: item for item in review.findings}
        selected_findings = [finding_by_id[item] for item in plan.finding_ids]
        review_message = self._latest_quality_review_message(state)
        revision_epoch = max(
            state.revision_count,
            state.revision_control.revision_epoch,
        ) + 1
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.RESEARCHER,
            target_layer=LayerId.RESEARCHER,
            revision_epoch=revision_epoch,
            source_review_id=review_message.message_id,
            source_finding_ids=plan.finding_ids,
        )
        report_id = str(state.research_report.get("research_report_id") or "")
        if not report_id:
            raise ValueError("Researcher internal Revision report has no research_report_id")
        request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.INTERNAL,
            source_layer=LayerId.RESEARCHER,
            target_layer=LayerId.RESEARCHER,
            revision_epoch=revision_epoch,
            root_revision_request_id=request_id,
            source_review_id=review_message.message_id,
            source_finding_ids=plan.finding_ids,
            target_agent_ids=plan.target_agent_ids,
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="researcher.research_report",
                    artifact_id=report_id,
                    sha256=canonical_sha256(state.research_report),
                )
            ],
            required_actions=list(
                dict.fromkeys(item.required_action for item in selected_findings)
            ),
            acceptance_conditions=[
                f"{item.finding_id} is resolved or explicitly retained as a limitation"
                for item in selected_findings
            ],
            evidence_expansion_allowed=True,
            retrieval_allowed=True,
            expected_human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            created_at=decision.decided_at,
        )
        message_id = str(uuid5(NAMESPACE_URL, f"{request_id}:request-message"))
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=review_message.message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id=plan.target_agent_ids[0],
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Execute an audited Researcher internal evidence Revision",
                    payload=request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="researcher.manager",
                        previous_stage=QUALITY_REVIEWER_ID,
                        next_stage=plan.target_agent_ids[0],
                    ),
                    routing=PMPRouting(
                        revision_target=plan.target_agent_ids[0],
                        reply_required=True,
                    ),
                    metadata=PMPMetadata(
                        created_at=decision.decided_at,
                        updated_at=decision.decided_at,
                        status=MessageStatus.REVISION_REQUIRED,
                        extensions={
                            "human_decision_id": decision.decision_id,
                            "role_definition": state.role_definition_usage[-1],
                        },
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
                "root_revision_request_id": request_id,
                "parent_revision_request_id": None,
                "pending_request_ids": list(
                    dict.fromkeys(
                        [*state.revision_control.pending_request_ids, request_id]
                    )
                ),
            }
        )
        self._record_common_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"request_written_{revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.RESEARCHER,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=message.message_id,
                artifact_ids=[report_id],
                reason=decision.reason,
                created_at=decision.decided_at,
            ),
        )
        return request

    @staticmethod
    def _upstream_plan_findings(review: ResearchQualityReviewOutput) -> list[Any]:
        return [
            finding
            for finding in review.findings
            if finding.finding_type
            == ResearchFindingType.UPSTREAM_PLAN_DEFECT.value
        ]

    def _route_producer_revision(
        self,
        state: ResearcherWorkflowState,
        *,
        review: ResearchQualityReviewOutput,
        review_message: PMPMessage,
    ) -> RevisionRequestV1:
        """Write one request-scoped Researcher -> Producer plan Revision."""

        findings = self._upstream_plan_findings(review)
        if not findings:
            raise ValueError("Producer Revision route requires an upstream plan defect")
        if state.research_report is None:
            raise ValueError("Producer Revision route requires the saved Research Report")
        plan_id = str(state.research_plan.get("research_plan_id") or "")
        report_id = str(state.research_report.get("research_report_id") or "")
        if not plan_id or not report_id:
            raise ValueError("Producer Revision base artifacts lack canonical IDs")

        revision_epoch = state.revision_control.revision_epoch + 1
        finding_ids = [finding.finding_id for finding in findings]
        request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.RESEARCHER,
            target_layer=LayerId.PRODUCER,
            revision_epoch=revision_epoch,
            source_review_id=review_message.message_id,
            source_finding_ids=finding_ids,
        )
        parent_request_id = (
            state.revision_control.active_request_id
            if state.revision_control.active_request_id
            and state.revision_control.phase
            in {
                RevisionControlPhase.COMPLETED.value,
                RevisionControlPhase.EXECUTING.value,
            }
            else None
        )
        root_request_id = (
            state.revision_control.root_revision_request_id
            if parent_request_id
            else request_id
        ) or request_id
        request = RevisionRequestV1.create(
            revision_request_id=request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.UPSTREAM,
            source_layer=LayerId.RESEARCHER,
            target_layer=LayerId.PRODUCER,
            revision_epoch=revision_epoch,
            root_revision_request_id=root_request_id,
            parent_revision_request_id=parent_request_id,
            source_review_id=review_message.message_id,
            source_finding_ids=finding_ids,
            target_agent_ids=["producer.research_planner"],
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="producer.research_plan",
                    artifact_id=plan_id,
                    sha256=canonical_sha256(state.research_plan),
                ),
                RevisionArtifactRef(
                    artifact_type="researcher.research_report",
                    artifact_id=report_id,
                    sha256=canonical_sha256(state.research_report),
                ),
            ],
            required_actions=list(
                dict.fromkeys(finding.required_action for finding in findings)
            ),
            acceptance_conditions=[
                f"{finding.finding_id} is resolved by an updated Research Plan"
                for finding in findings
            ],
            evidence_expansion_allowed=False,
            retrieval_allowed=False,
            expected_human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            created_at=review_message.metadata.updated_at,
        )
        message_id = str(uuid5(NAMESPACE_URL, f"{request_id}:request-message"))
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=review_message.message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id="producer.manager",
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Revise the upstream Research Plan without fabricating evidence",
                    payload=request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="researcher.quality_review",
                        previous_stage=QUALITY_REVIEWER_ID,
                        next_stage="producer.manager",
                    ),
                    routing=PMPRouting(
                        revision_target="producer.manager",
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
        self.revision_exchange.create_request_once(
            message,
            budget_policy=RevisionBudgetPolicy(
                internal_limit=self.max_revisions,
                upstream_limit=self.max_revisions,
            ),
        )
        if not any(item.message_id == message.message_id for item in state.message_history):
            state.message_history.append(message)
        state.revision_control = RevisionControlState.model_validate(
            {
                **state.revision_control.model_dump(mode="json"),
                "phase": RevisionControlPhase.WAITING_UPSTREAM_RESULT.value,
                "revision_epoch": revision_epoch,
                "active_request_id": request_id,
                "active_request_message_id": message.message_id,
                "active_result_id": None,
                "root_revision_request_id": root_request_id,
                "parent_revision_request_id": parent_request_id,
                "pending_request_ids": list(
                    dict.fromkeys([*state.revision_control.pending_request_ids, request_id])
                ),
            }
        )
        state.status = WorkflowStatus.WAITING_UPSTREAM_REVISION
        state.current_agent_ids = []
        state.completed_at = None
        state.error = None
        self._record_common_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"upstream_request_written_{revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
                layer=LayerId.RESEARCHER,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=message.message_id,
                artifact_ids=[plan_id, report_id],
                reason=review.reason,
                created_at=review_message.metadata.updated_at,
            ),
        )
        self.repository.save(state)
        return request

    def decide_human_evidence(
        self,
        workflow_id: str,
        decision: HumanEvidenceDecisionType | str,
        *,
        reason: str,
        actor_source: HumanActorSource | str,
    ) -> ResearcherWorkflowState:
        state = self.repository.load(workflow_id)
        if state.human_evidence_decision is not None:
            raise ValueError("Human evidence decision already exists for this Quality Review")
        if any(
            artifact.decision.quality_review_id
            == self._latest_quality_review_message(state).message_id
            for artifact in self.repository.list_human_evidence_decisions(workflow_id)
        ):
            raise ValueError("Human evidence decision already exists for this Quality Review")
        summary = self._prepare_human_evidence_gate(state)
        if not summary.eligible:
            raise ValueError("Human Evidence Gate cannot override integrity failures")

        decision_type = HumanEvidenceDecisionType(decision)
        sufficiency_ids = [
            item.finding_id for item in summary.evidence_sufficiency_findings
        ]
        if decision_type == HumanEvidenceDecisionType.ACCEPT and sufficiency_ids:
            raise ValueError(
                "ACCEPT is allowed only when no Evidence Sufficiency Finding remains; "
                "use ACCEPT_WITH_LIMITATIONS or REVISE"
            )
        if (
            decision_type == HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
            and not sufficiency_ids
        ):
            raise ValueError("ACCEPT_WITH_LIMITATIONS requires an Evidence Sufficiency Finding")
        if decision_type == HumanEvidenceDecisionType.REVISE and not sufficiency_ids:
            raise ValueError("REVISE requires an Evidence Sufficiency Finding")

        gate_revision = len(state.human_evidence_decision_history) + 1
        decision_record = HumanEvidenceDecision(
            decision_id=self._stable_human_evidence_id(
                "human_evidence_decision",
                workflow_id,
                summary.quality_review_id,
                gate_revision,
            ),
            workflow_id=workflow_id,
            quality_review_id=summary.quality_review_id,
            evidence_set_sha256=summary.evidence_set_sha256,
            human_gate_revision=gate_revision,
            decision=decision_type,
            accepted_finding_ids=(
                sufficiency_ids
                if decision_type == HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS
                else []
            ),
            revision_requested_finding_ids=(
                sufficiency_ids if decision_type == HumanEvidenceDecisionType.REVISE else []
            ),
            reason=reason.strip(),
            actor_source=HumanActorSource(actor_source),
            provider_calls_authorized=False,
        )
        revision_plan = (
            self._revision_plan(state, summary, decision_record)
            if decision_type == HumanEvidenceDecisionType.REVISE
            else None
        )
        decision_message = self._decision_message(state, decision_record)
        artifact = HumanEvidenceDecisionArtifact(
            decision=decision_record,
            decision_message=decision_message,
        )
        try:
            self.repository.create_human_evidence_decision_once(artifact)
        except ValueError:
            # A concurrent CLI/Discord process may have won after our initial
            # read. Reconcile its durable decision so this loser cannot leave a
            # stale WAITING state behind, then still report the duplicate.
            self.recover_human_evidence_gate(workflow_id)
            raise

        state.human_evidence_decision = decision_record
        state.human_evidence_decision_history.append(decision_record)
        state.message_history.append(decision_message)
        state.accepted_evidence_gaps = self._accepted_gaps(summary, decision_record)
        if decision_type == HumanEvidenceDecisionType.REVISE:
            state.evidence_revision_plan = revision_plan
            self._plan_internal_revision_request(
                state,
                decision=decision_record,
                plan=revision_plan,
            )
            state.status = WorkflowStatus.BLOCKED
            state.error = {
                "code": "EVIDENCE_REVISION_PROVIDER_AUTHORIZATION_REQUIRED",
                "message": (
                    "Human requested evidence revision; explicit Provider call "
                    "authorization is required before execution"
                ),
            }
            state.completed_at = None
            self.repository.save(state)
            return state

        state.evidence_revision_plan = None
        state.status = WorkflowStatus.FINALIZING
        state.error = None
        self.repository.save(state)
        return self._finalize_human_evidence_decision(state)

    async def revise(
        self,
        workflow_id: str,
        *,
        reason: str = "Human Operator requested additional evidence research",
        actor_source: HumanActorSource | str = HumanActorSource.CLI,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        state = self.decide_human_evidence(
            workflow_id,
            HumanEvidenceDecisionType.REVISE,
            reason=reason,
            actor_source=actor_source,
        )
        await self._emit(
            progress_callback,
            "Human Evidence Gate: REVISE recorded; Provider authorization required",
        )
        return state

    def recover_human_evidence_gate(
        self,
        workflow_id: str,
    ) -> ResearcherWorkflowState:
        state = self.repository.load(workflow_id)
        summary = self._gate_summary(state, apply_repairs=False)
        try:
            artifact = self.repository.load_human_evidence_decision(
                workflow_id,
                summary.quality_review_id,
            )
        except FileNotFoundError:
            if state.human_evidence_decision is not None:
                raise ValueError(
                    "Workflow state claims a Human Evidence Decision but its artifact is missing"
                )
            self._prepare_human_evidence_gate(state)
            return state

        if artifact.decision.evidence_set_sha256 != summary.evidence_set_sha256:
            raise ValueError("Human Evidence Decision does not match the current Evidence Set")
        if state.human_evidence_decision is None:
            state.human_evidence_decision = artifact.decision
            if not any(
                item.decision_id == artifact.decision.decision_id
                for item in state.human_evidence_decision_history
            ):
                state.human_evidence_decision_history.append(artifact.decision)
            if not any(
                item.message_id == artifact.decision_message.message_id
                for item in state.message_history
            ):
                state.message_history.append(artifact.decision_message)
            state.accepted_evidence_gaps = self._accepted_gaps(
                summary, artifact.decision
            )
            if artifact.decision.decision == HumanEvidenceDecisionType.REVISE.value:
                state.evidence_revision_plan = self._revision_plan(
                    state, summary, artifact.decision
                )
                if state.revision_control.phase == RevisionControlPhase.IDLE.value:
                    self._plan_internal_revision_request(
                        state,
                        decision=artifact.decision,
                        plan=state.evidence_revision_plan,
                    )
            self.repository.save(state)

        if artifact.decision.decision == HumanEvidenceDecisionType.REVISE.value:
            state.status = WorkflowStatus.BLOCKED
            state.error = {
                "code": "EVIDENCE_REVISION_PROVIDER_AUTHORIZATION_REQUIRED",
                "message": "Human requested evidence revision; Provider authorization required",
            }
            self.repository.save(state)
            return state
        return self._finalize_human_evidence_decision(state)

    def _finalize_human_evidence_decision(
        self,
        state: ResearcherWorkflowState,
    ) -> ResearcherWorkflowState:
        decision = state.human_evidence_decision
        if decision is None or decision.decision not in {
            HumanEvidenceDecisionType.ACCEPT.value,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS.value,
        }:
            raise ValueError("A handoff-permitting Human Evidence Decision is required")
        report = ResearchReport.model_validate(state.research_report)
        decision_message = next(
            (
                message
                for message in reversed(state.message_history)
                if message.message_type == MessageType.HUMAN_EVIDENCE_DECISION.value
                and message.payload.get("decision_id") == decision.decision_id
            ),
            None,
        )
        if decision_message is None:
            raise ValueError("Human Evidence Decision PMP is missing")

        try:
            if state.pending_revision_parent_message_id:
                request = self.repository.load_revision_request(state.workflow_id)
                reply = self._send_revision_result_to_deliberation(state, report, request)
                if state.external_revision_history:
                    state.external_revision_history[-1].status = "reply_sent"
                    state.external_revision_history[-1].completed_at = utc_now()
                    state.external_revision_history[-1].reply_message_id = reply.message_id
                state.external_revision_reply_sent = True
                state.external_revision_status = ExternalRevisionCheckpoint.COMPLETED_REVISION
                state.pending_external_revision_request_ids = []
                state.pending_revision_parent_message_id = None
                state.pending_revision_source_agent_id = None
                state.status = WorkflowStatus.COMPLETED_REVISION
            else:
                self._send_to_deliberation(state, report, decision_message.message_id)
                state.deliberation_sent = True
                state.status = WorkflowStatus.COMPLETED
        except Exception as exc:
            state.status = WorkflowStatus.FAILED
            state.error = {
                "code": "HUMAN_EVIDENCE_HANDOFF_INCOMPLETE",
                "message": str(exc),
            }
            state.completed_at = None
            self.repository.save(state)
            return state
        state.current_agent_ids = []
        state.completed_at = utc_now()
        state.error = None
        self.repository.save(state)
        return state

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
        if (
            state.revision_control.phase
            == RevisionControlPhase.WAITING_UPSTREAM_RESULT.value
        ):
            return self._consume_producer_revision_result(state)
        if state.research_report is None:
            raise ValueError("Researcher workflow has no saved Research Report")
        request = self.repository.load_revision_request(workflow_id)
        payload = self._validate_external_revision_request(state, request)
        request_ids = [item.revision_request_id for item in payload.revision_requests]
        canonical_request = self._canonical_deliberation_revision_request(
            state,
            request_ids=request_ids,
        )
        if canonical_request is not None:
            canonical_payload = RevisionRequestV1.model_validate(
                canonical_request.payload
            )
            owned_artifacts = [
                item
                for item in canonical_payload.base_artifacts
                if item.artifact_type == "researcher.research_report"
            ]
            self.revision_exchange.validator.validate_current_base_artifacts(
                canonical_payload.model_copy(
                    update={"base_artifacts": owned_artifacts}
                ),
                {
                    (
                        "researcher.research_report",
                        str(state.research_report.get("research_report_id") or ""),
                    ): canonical_sha256(state.research_report)
                },
            )
            if not any(
                item.message_id == canonical_request.message_id
                for item in state.message_history
            ):
                state.message_history.append(canonical_request)
        if self._reconcile_written_external_reply(state, request, request_ids):
            self.repository.save(state)
            await self._emit(
                progress_callback,
                "Existing research_revision_result detected; completion state restored",
            )
            return state
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
            state.external_revision_reply_sent = False
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
            state.external_revision_status = ExternalRevisionCheckpoint.REQUEST_RECEIVED
            self.repository.save(state)
            state.research_tasks.extend(
                task.model_dump(mode="json") for task in revision_tasks
            )
            if not any(
                message.message_id == request.message_id
                for message in state.message_history
            ):
                state.message_history.append(request)
            state.external_revision_status = ExternalRevisionCheckpoint.TASKS_PLANNED
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
        self._validate_external_checkpoint(
            state,
            revision_tasks,
            completed_task_ids,
        )
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
            state.external_revision_status = ExternalRevisionCheckpoint.TASKS_DISPATCHED
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
        resume_checkpoint = state.external_revision_status
        advanced_checkpoints = {
            ExternalRevisionCheckpoint.REPORT_INTEGRATED.value,
            ExternalRevisionCheckpoint.QUALITY_REVIEWING.value,
            ExternalRevisionCheckpoint.QUALITY_REVIEWED.value,
            ExternalRevisionCheckpoint.REPLY_READY.value,
        }
        if resume_checkpoint not in advanced_checkpoints:
            state.external_revision_status = ExternalRevisionCheckpoint.RESULTS_COLLECTED
        self.repository.save(state)
        return await self._integrate_and_review(
            state,
            progress_callback,
            external_request=request,
            reuse_external_report=resume_checkpoint in advanced_checkpoints,
        )

    def _canonical_deliberation_revision_request(
        self,
        state: ResearcherWorkflowState,
        *,
        request_ids: list[str],
    ) -> PMPMessage | None:
        expected_legacy_ids = set(request_ids)
        legacy_payload = self.repository.load_revision_request(
            state.workflow_id
        ).payload
        expected_finding_ids = {
            finding_id
            for item in legacy_payload.get("revision_requests", [])
            if item.get("revision_request_id") in expected_legacy_ids
            for finding_id in item.get("source_finding_ids", [])
        }
        matches: list[PMPMessage] = []
        for message in self.revision_exchange.list_requests(
            target_layer=LayerId.RESEARCHER,
            workflow_id=state.workflow_id,
        ):
            request = self.revision_exchange.validator.validate_request_message(
                message
            )
            if (
                request.source_layer == LayerId.DELIBERATION.value
                and request.target_layer == LayerId.RESEARCHER.value
                and request.route == RevisionRoute.UPSTREAM.value
                and set(request.source_finding_ids) == expected_finding_ids
            ):
                matches.append(message)
        if len(matches) > 1:
            raise ValueError("Multiple canonical Deliberation Revision Requests are pending")
        return matches[0] if matches else None

    def _consume_producer_revision_result(
        self,
        state: ResearcherWorkflowState,
    ) -> ResearcherWorkflowState:
        """Consume one correlated Producer result and plan a separate local refresh."""

        request_id = state.revision_control.active_request_id
        if not request_id:
            raise ValueError("Researcher waiting state has no upstream request identity")
        request_message = self.revision_exchange.load_request(
            target_layer=LayerId.PRODUCER,
            workflow_id=state.workflow_id,
            revision_request_id=request_id,
        )
        request = self.revision_exchange.validator.validate_request_message(
            request_message
        )
        result_message = self.revision_exchange.load_result(
            requester_layer=LayerId.RESEARCHER,
            workflow_id=state.workflow_id,
            revision_request_id=request_id,
            request_message=request_message,
        )
        result = RevisionResultV1.model_validate(result_message.payload)
        if result.revision_result_id in state.revision_control.consumed_result_ids:
            return state
        if result.status != RevisionExecutionStatus.COMPLETED.value:
            state.revision_control.phase = RevisionControlPhase.BLOCKED
            state.status = WorkflowStatus.BLOCKED
            state.error = {
                "code": "UPSTREAM_REVISION_INCOMPLETE",
                "message": "Producer Revision Result did not resolve every plan defect",
            }
            self.repository.save(state)
            return state
        if state.research_report is None:
            raise ValueError("Researcher upstream resume requires its saved Research Report")
        current_hashes = {
            (
                "producer.research_plan",
                str(state.research_plan.get("research_plan_id") or ""),
            ): canonical_sha256(state.research_plan),
            (
                "researcher.research_report",
                str(state.research_report.get("research_report_id") or ""),
            ): canonical_sha256(state.research_report),
        }
        self.revision_exchange.validator.validate_current_base_artifacts(
            request,
            current_hashes,
        )

        handoff = self.repository.load_producer_handoff(state.workflow_id)
        updated_plan = self._validate_producer_handoff(handoff)
        updated_plan_payload = updated_plan.model_dump(mode="json")
        plan_artifact = next(
            (
                item
                for item in result.result_artifacts
                if item.artifact_type == "producer.research_plan"
            ),
            None,
        )
        if (
            plan_artifact is None
            or plan_artifact.artifact_id != updated_plan.research_plan_id
            or plan_artifact.sha256 != canonical_sha256(updated_plan_payload)
        ):
            raise ValueError(
                "Producer Revision Result does not match the updated Research Plan handoff"
            )

        old_tasks = [
            ResearchTask.model_validate(item)
            for item in state.research_tasks
            if not item.get("revision_context")
        ]
        new_tasks = self._create_research_tasks(updated_plan)

        def task_signature(task: ResearchTask) -> tuple[str, str, str, str, str]:
            return (
                task.research_question_id,
                task.target_agent_id,
                task.question,
                canonical_sha256(task.scope),
                canonical_sha256(task.constraints),
            )

        old_signatures = {task_signature(task) for task in old_tasks}
        affected = [task for task in new_tasks if task_signature(task) not in old_signatures]
        child_epoch = request.revision_epoch + 1
        child_request_id = deterministic_revision_request_id(
            workflow_id=state.workflow_id,
            source_layer=LayerId.RESEARCHER,
            target_layer=LayerId.RESEARCHER,
            revision_epoch=child_epoch,
            source_review_id=result_message.message_id,
            source_finding_ids=request.source_finding_ids,
        )
        suffix = child_request_id.rsplit("_", 1)[-1][:12]
        revision_tasks: list[ResearchTask] = []
        for task in affected:
            data = task.model_dump(mode="json")
            agent_name = task.target_agent_id.split(".", 1)[-1]
            data["task_id"] = (
                f"research_plan_revision_{child_epoch}_{suffix}_{agent_name}_"
                f"{task.research_question_id[:24]}"
            )
            data["revision_context"] = {
                "revision_request_id": child_request_id,
                "parent_revision_request_id": request.revision_request_id,
                "revision_iteration": child_epoch,
                "external_revision_iteration": state.external_revision_count,
                "explicit_operator_revision": True,
                "issue": "Upstream Research Plan changed",
                "required_action": "Refresh only tasks affected by the updated plan",
            }
            revision_tasks.append(ResearchTask.model_validate(data))
        target_agent_ids = list(
            dict.fromkeys(task.target_agent_id for task in revision_tasks)
        ) or [self.agent_id]
        child_request = RevisionRequestV1.create(
            revision_request_id=child_request_id,
            workflow_id=state.workflow_id,
            route=RevisionRoute.INTERNAL,
            source_layer=LayerId.RESEARCHER,
            target_layer=LayerId.RESEARCHER,
            revision_epoch=child_epoch,
            root_revision_request_id=request.root_revision_request_id,
            parent_revision_request_id=request.revision_request_id,
            source_review_id=result_message.message_id,
            source_finding_ids=request.source_finding_ids,
            target_agent_ids=target_agent_ids,
            base_artifacts=[
                RevisionArtifactRef(
                    artifact_type="producer.research_plan",
                    artifact_id=updated_plan.research_plan_id,
                    sha256=canonical_sha256(updated_plan_payload),
                ),
                RevisionArtifactRef(
                    artifact_type="researcher.research_report",
                    artifact_id=str(state.research_report["research_report_id"]),
                    sha256=canonical_sha256(state.research_report),
                ),
            ],
            required_actions=[
                "Refresh Researcher outputs affected by the revised Research Plan"
            ],
            acceptance_conditions=[
                f"{finding_id} is re-evaluated against the revised Research Plan"
                for finding_id in request.source_finding_ids
            ],
            evidence_expansion_allowed=bool(revision_tasks),
            retrieval_allowed=bool(revision_tasks),
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
                    receiver_agent_id=target_agent_ids[0],
                    message_type=MessageType.REVISION_REQUEST,
                    objective="Refresh Researcher outputs after an upstream plan Revision",
                    payload=child_request.model_dump(mode="json"),
                    context=PMPContext(
                        current_stage="researcher.manager",
                        previous_stage="producer.manager",
                        next_stage=target_agent_ids[0],
                    ),
                    routing=PMPRouting(
                        revision_target=target_agent_ids[0],
                        reply_required=True,
                    ),
                    metadata=PMPMetadata(
                        created_at=result.completed_at,
                        updated_at=result.completed_at,
                        status=MessageStatus.REVISION_REQUIRED,
                        extensions={"role_definition": state.role_definition_usage[-1]},
                    ),
                ).model_dump(mode="json"),
                "message_id": child_message_id,
            }
        )
        self.revision_exchange.create_internal_request_once(child_message)
        for message in (result_message, handoff, child_message):
            if not any(item.message_id == message.message_id for item in state.message_history):
                state.message_history.append(message)
        existing_task_ids = {str(item.get("task_id")) for item in state.research_tasks}
        state.research_tasks.extend(
            task.model_dump(mode="json")
            for task in revision_tasks
            if task.task_id not in existing_task_ids
        )
        state.producer_handoff = handoff.model_dump(mode="json")
        state.research_plan = updated_plan_payload
        state.human_evidence_decision = None
        state.accepted_evidence_gaps = []
        state.evidence_revision_plan = None
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
        state.current_agent_ids = []
        state.completed_at = None
        state.error = {
            "code": "RESEARCH_PLAN_REFRESH_AUTHORIZATION_REQUIRED",
            "message": (
                "Updated Research Plan consumed; explicit authorization is required "
                "before affected Researcher tasks are refreshed"
            ),
        }
        for event in (
            RevisionAuditEvent(
                audit_event_id=f"result_consumed_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.RESEARCHER,
                event_type=RevisionAuditEventType.RESULT_CONSUMED,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[updated_plan.research_plan_id],
                reason="Correlated Producer Revision Result and handoff were validated",
                created_at=result.completed_at,
            ),
            RevisionAuditEvent(
                audit_event_id=f"request_written_{child_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=child_request_id,
                layer=LayerId.RESEARCHER,
                event_type=RevisionAuditEventType.REQUEST_WRITTEN,
                actor_id=self.agent_id,
                message_id=child_message.message_id,
                artifact_ids=[updated_plan.research_plan_id],
                reason="Downstream refresh is independently authorized from Producer Revision",
                created_at=result.completed_at,
            ),
        ):
            self._record_common_revision_audit(state, event)
        self.repository.save(state)
        return state

    def authorize_provider_retry(
        self,
        workflow_id: str,
    ) -> ProviderRetryAuthorization:
        """Authorize the latest retryable Quality Reviewer call exactly once."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Operator provider retry is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        retryable_states = {
            WorkflowStatus.FAILED.value,
            WorkflowStatus.INTEGRATING.value,
            WorkflowStatus.REVIEWING.value,
        }
        if state.status not in retryable_states:
            raise ValueError(
                "Researcher must be FAILED or resuming the saved Quality Review "
                "checkpoint before an operator provider retry"
            )
        if state.external_revision_status != ExternalRevisionCheckpoint.QUALITY_REVIEWING.value:
            raise ValueError(
                "Operator provider retry is allowed only from the external Quality Review checkpoint"
            )

        error_response = next(
            (
                message
                for message in reversed(state.message_history)
                if message.sender_agent_id == QUALITY_REVIEWER_ID
                and message.receiver_agent_id == self.agent_id
                and message.message_type == MessageType.ERROR.value
            ),
            None,
        )
        if error_response is None:
            raise ValueError("No persisted Quality Reviewer error was found")
        original_task_id = error_response.payload.get("task_id")
        source_error_class = error_response.payload.get("error_class")
        if not isinstance(original_task_id, str) or not original_task_id:
            raise ValueError("Quality Reviewer error has no logical task ID")
        if source_error_class != "RetryableAgentError":
            raise ValueError(
                "Operator provider retry is allowed only for RetryableAgentError"
            )

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
            or request.sender_agent_id != self.agent_id
            or request.receiver_agent_id != QUALITY_REVIEWER_ID
            or request.message_type != MessageType.TASK.value
            or request.payload.get("task_id") != original_task_id
        ):
            raise ValueError("Quality Reviewer error is not correlated to its saved request")

        reviewer = self.registry.get(QUALITY_REVIEWER_ID)
        provider_id = getattr(reviewer.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            raise ValueError("Quality Reviewer provider has no stable logical provider ID")
        return self.provider_retry_store.authorize_once(
            workflow_id=workflow_id,
            provider_id=provider_id,
            agent_id=QUALITY_REVIEWER_ID,
            original_task_id=original_task_id,
            source_error_message_id=error_response.message_id,
            source_error_class=source_error_class,
        )

    async def retry_provider_call(
        self,
        workflow_id: str,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        authorization = self.authorize_provider_retry(workflow_id)
        await self._emit(
            progress_callback,
            "Operator one-time provider retry authorized: "
            + authorization.retry_task_id,
        )
        return await self.resume(
            workflow_id,
            progress_callback=progress_callback,
        )

    async def execute_authorized_revision(
        self,
        workflow_id: str,
        *,
        actor_id: str = "cli.operator",
        actor_source: str = "CLI",
        authorization_reason: str = (
            "Operator authorized one saved Researcher evidence Revision cycle"
        ),
        progress_callback: ProgressCallback | None = None,
    ) -> ResearcherWorkflowState:
        """Execute a separately authorized Provider revision after Human REVISE."""

        if not self.demo_safe_mode:
            raise ValueError(
                "Explicit Researcher revision is available only while Demo Safe Mode is enabled"
            )
        state = self.repository.load(workflow_id)
        request_message = self._active_common_revision_request(state)
        common_request = RevisionRequestV1.model_validate(request_message.payload)
        human_evidence_revision = (
            state.human_evidence_decision is not None
            and state.human_evidence_decision.decision
            == HumanEvidenceDecisionType.REVISE.value
            and state.evidence_revision_plan is not None
        )
        plan_refresh_revision = (
            common_request.route == RevisionRoute.INTERNAL.value
            and common_request.parent_revision_request_id is not None
            and any(
                item.artifact_type == "producer.research_plan"
                for item in common_request.base_artifacts
            )
        )
        if not human_evidence_revision and not plan_refresh_revision:
            raise ValueError(
                "Provider revision requires a Human REVISE plan or a correlated upstream plan refresh"
            )
        if state.research_report is None:
            raise ValueError("Researcher workflow has no saved Research Report")
        self.revision_exchange.validator.validate_current_base_artifacts(
            common_request,
            {
                (
                    "researcher.research_report",
                    str(state.research_report.get("research_report_id") or ""),
                ): canonical_sha256(state.research_report),
                (
                    "producer.research_plan",
                    str(state.research_plan.get("research_plan_id") or ""),
                ): canonical_sha256(state.research_plan),
            },
        )
        try:
            external_request = self.repository.load_revision_request(workflow_id)
            self._validate_external_revision_request(state, external_request)
        except FileNotFoundError:
            external_request = None
        review = self._saved_quality_review(state)
        if review.status != "revision_required":
            raise ValueError(
                "Explicit Researcher revision requires revision_required review_result"
            )

        revision_tasks = self._saved_internal_revision_tasks(state)
        manager_only = common_request.target_agent_ids == [self.agent_id]
        budget = self.revision_exchange.budget_store.for_request(
            workflow_id=state.workflow_id,
            layer=LayerId.RESEARCHER,
            route=RevisionRoute.INTERNAL,
            revision_request_id=common_request.revision_request_id,
        )
        if budget is None:
            if state.status != WorkflowStatus.BLOCKED.value:
                raise ValueError(
                    "Researcher must be BLOCKED before a new explicit revision cycle"
                )
            try:
                budget = self.revision_exchange.budget_store.consume(
                    policy=RevisionBudgetPolicy(
                        internal_limit=self.max_revisions,
                        upstream_limit=self.max_revisions,
                    ),
                    workflow_id=state.workflow_id,
                    layer=LayerId.RESEARCHER,
                    route=RevisionRoute.INTERNAL,
                    revision_request_id=common_request.revision_request_id,
                )
            except RevisionBudgetExhausted as exc:
                state.revision_control.phase = RevisionControlPhase.BLOCKED
                state.status = WorkflowStatus.BLOCKED
                state.error = {"code": "REVISION_BUDGET_EXHAUSTED", "message": str(exc)}
                self.repository.save(state)
                return state
            state.revision_count = max(state.revision_count, budget.iteration)
            if not plan_refresh_revision:
                report = self._build_report(state)
                missing_targets = [
                    RESEARCH_TARGET_MAP[ResearchTarget(gap.missing_category)]
                    for gap in report.evidence_gaps
                ]
                revision_targets = list(
                    dict.fromkeys(review.revision_targets + missing_targets)
                )
                explicit_review = review.model_copy(
                    update={"revision_targets": revision_targets}
                )
                state.revision_history.append(
                    ResearchRevisionRecord(
                        iteration=state.revision_count,
                        target_agent_ids=revision_targets,
                        findings=[
                            finding.model_dump(mode="json")
                            for finding in review.findings
                        ],
                    )
                )
                revision_tasks = self._build_revision_tasks(
                    state,
                    explicit_review,
                    revision_targets=revision_targets,
                )
                existing_task_ids = {
                    str(item.get("task_id")) for item in state.research_tasks
                }
                state.research_tasks.extend(
                    task.model_dump(mode="json")
                    for task in revision_tasks
                    if task.task_id not in existing_task_ids
                )
            elif not manager_only and not revision_tasks:
                raise ValueError(
                    "Upstream plan refresh lost its persisted affected Researcher tasks"
                )
            if plan_refresh_revision:
                state.revision_history.append(
                    ResearchRevisionRecord(
                        iteration=state.revision_count,
                        target_agent_ids=common_request.target_agent_ids,
                        findings=[
                            {
                                "finding_id": finding_id,
                                "finding_type": "UPSTREAM_PLAN_DEFECT",
                                "required_action": common_request.required_actions[0],
                            }
                            for finding_id in common_request.source_finding_ids
                        ],
                    )
                )
            if state.external_revision_history:
                state.external_revision_history[-1].status = "processing"
            state.status = WorkflowStatus.REVISING
            state.error = None
            state.completed_at = None
            self.repository.save(state)
        elif state.status not in {
            WorkflowStatus.REVISING.value,
            WorkflowStatus.FAILED.value,
            WorkflowStatus.DISPATCHING.value,
            WorkflowStatus.COLLECTING.value,
            WorkflowStatus.PARTIALLY_COMPLETED.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError(
                "Saved explicit revision tasks can resume only from REVISING or FAILED"
            )

        provider_reservation_ids = [task.task_id for task in revision_tasks]
        provider_reservation_ids.append(self._quality_review_task_id(
            state,
            state.external_revision_history[-1]
            if external_request is not None and state.external_revision_history
            else None,
        ))
        retrieval_reservation_ids = self._revision_retrieval_ids(
            state,
            revision_tasks,
        )
        authorization = self._authorize_common_revision_execution(
            state,
            common_request,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=authorization_reason,
            max_provider_calls=len(provider_reservation_ids),
            max_retrieval_calls=len(retrieval_reservation_ids),
        )
        consumed = self.revision_exchange.consume_authorization(
            authorization,
            provider_reservation_ids=provider_reservation_ids,
            retrieval_reservation_ids=retrieval_reservation_ids,
        )
        state.revision_control.phase = RevisionControlPhase.EXECUTING
        self._record_common_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"authorization_consumed_{common_request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=common_request.revision_request_id,
                layer=LayerId.RESEARCHER,
                event_type=RevisionAuditEventType.AUTHORIZATION_CONSUMED,
                actor_id=actor_id,
                reservation_ids=[
                    *provider_reservation_ids,
                    *retrieval_reservation_ids,
                ],
                reason=authorization_reason,
                created_at=consumed.consumed_at,
            ),
        )
        self.repository.save(state)

        completed_task_ids = self._completed_research_task_ids(state)
        incomplete_tasks = [
            task for task in revision_tasks if task.task_id not in completed_task_ids
        ]
        if incomplete_tasks:
            completed = await self._execute_tasks(
                state,
                incomplete_tasks,
                is_revision=True,
                progress_callback=progress_callback,
            )
            if completed < len(incomplete_tasks):
                return await self._fail(
                    state,
                    "Explicit Researcher revision has incomplete specialist tasks",
                    progress_callback,
                )
        else:
            await self._emit(
                progress_callback,
                "Saved explicit Researcher revision results detected; redispatch skipped",
            )
        if external_request is not None:
            state.external_revision_status = ExternalRevisionCheckpoint.REPORT_INTEGRATING
        self.repository.save(state)
        return await self._integrate_and_review(
            state,
            progress_callback,
            external_request=external_request,
            reuse_external_report=False,
        )

    @staticmethod
    def _saved_internal_revision_tasks(
        state: ResearcherWorkflowState,
    ) -> list[ResearchTask]:
        active_request_id = state.revision_control.active_request_id
        return [
            ResearchTask.model_validate(raw_task)
            for raw_task in state.research_tasks
            if (raw_task.get("revision_context") or {}).get(
                "explicit_operator_revision"
            )
            and (
                (raw_task.get("revision_context") or {}).get(
                    "revision_request_id"
                )
                == active_request_id
                if (raw_task.get("revision_context") or {}).get(
                    "revision_request_id"
                )
                else (raw_task.get("revision_context") or {}).get(
                    "revision_iteration"
                )
                == state.revision_count
            )
        ]

    def _active_common_revision_request(
        self,
        state: ResearcherWorkflowState,
    ) -> PMPMessage:
        message_id = state.revision_control.active_request_message_id
        request_id = state.revision_control.active_request_id
        if not message_id or not request_id:
            raise ValueError("Researcher has no active canonical Revision Request")
        message = next(
            (item for item in state.message_history if item.message_id == message_id),
            None,
        )
        if message is None:
            message = self.revision_exchange.load_internal_request(
                layer=LayerId.RESEARCHER,
                workflow_id=state.workflow_id,
                revision_request_id=request_id,
            )
        request = self.revision_exchange.validator.validate_request_message(message)
        if request.revision_request_id != request_id:
            raise ValueError("Researcher active Revision identity is inconsistent")
        return message

    def _authorize_common_revision_execution(
        self,
        state: ResearcherWorkflowState,
        request: RevisionRequestV1,
        *,
        actor_id: str,
        actor_source: str,
        reason: str,
        max_provider_calls: int,
        max_retrieval_calls: int,
    ) -> RevisionExecutionAuthorization:
        try:
            existing = self.revision_exchange.load_authorization(
                executing_layer=LayerId.RESEARCHER,
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
                or existing.max_provider_calls != max_provider_calls
                or existing.max_retrieval_calls != max_retrieval_calls
            ):
                raise ValueError(
                    "Researcher Revision is already authorized with a different identity"
                )
            return existing
        authorization = RevisionExecutionAuthorization(
            authorization_id=(
                "revision_authorization_"
                + uuid5(NAMESPACE_URL, request.revision_request_id).hex
            ),
            workflow_id=state.workflow_id,
            revision_request_id=request.revision_request_id,
            executing_layer=LayerId.RESEARCHER,
            actor_id=actor_id,
            actor_source=actor_source,
            reason=reason,
            max_provider_calls=max_provider_calls,
            max_retrieval_calls=max_retrieval_calls,
        )
        self.revision_exchange.create_authorization_once(authorization)
        self._record_common_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"authorization_created_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.RESEARCHER,
                event_type=RevisionAuditEventType.AUTHORIZATION_CREATED,
                actor_id=actor_id,
                reason=reason,
                created_at=authorization.created_at,
            ),
        )
        return authorization

    def _revision_retrieval_ids(
        self,
        state: ResearcherWorkflowState,
        tasks: list[ResearchTask],
    ) -> list[str]:
        identities: list[str] = []
        for task in tasks:
            coordinator = self.registry.get(task.target_agent_id).retrieval_coordinator
            if coordinator is None:
                raise ValueError(
                    f"Researcher Revision target has no Retrieval coordinator: {task.target_agent_id}"
                )
            identities.append(
                coordinator.retrieval_identity(
                    workflow_id=state.workflow_id,
                    task_id=task.task_id,
                    agent_id=task.target_agent_id,
                    strategy=RetrievalStrategy(task.research_target),
                )
            )
        return identities

    def _finalize_common_internal_revision(
        self,
        state: ResearcherWorkflowState,
        *,
        review_response: PMPMessage,
        review: ResearchQualityReviewOutput,
    ) -> None:
        request_message = self._active_common_revision_request(state)
        request = RevisionRequestV1.model_validate(request_message.payload)
        if request.route != RevisionRoute.INTERNAL.value:
            return
        if state.research_report is None:
            raise ValueError("Researcher Revision result requires a saved Research Report")
        report_id = str(state.research_report.get("research_report_id") or "")
        result_artifact = RevisionArtifactRef(
            artifact_type="researcher.research_report",
            artifact_id=report_id,
            sha256=canonical_sha256(state.research_report),
        )
        completed = review.status in {"approved", "approved_with_conditions"}
        result_id = "revision_result_" + uuid5(
            NAMESPACE_URL,
            f"{request.revision_request_id}:result",
        ).hex
        revision_tasks = self._saved_internal_revision_tasks(state)
        provider_ids = [task.task_id for task in revision_tasks]
        provider_ids.append(
            self._quality_review_task_id(
                state,
                state.external_revision_history[-1]
                if state.external_revision_history
                else None,
            )
        )
        retrieval_ids = self._revision_retrieval_ids(state, revision_tasks)
        result = RevisionResultV1.create(
            revision_result_id=result_id,
            revision_request_id=request.revision_request_id,
            request_message_id=request_message.message_id,
            workflow_id=state.workflow_id,
            requester_layer=LayerId.RESEARCHER,
            producer_layer=LayerId.RESEARCHER,
            revision_epoch=request.revision_epoch,
            status=(
                RevisionExecutionStatus.COMPLETED
                if completed
                else RevisionExecutionStatus.PARTIAL
            ),
            base_artifacts=request.base_artifacts,
            result_artifacts=[result_artifact],
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id=finding_id,
                    outcome=(
                        RevisionFindingOutcome.RESOLVED
                        if completed
                        else RevisionFindingOutcome.UNRESOLVED
                    ),
                    reason=review.reason,
                    result_artifact_ids=[report_id],
                )
                for finding_id in request.source_finding_ids
            ],
            human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            provider_reservation_ids=provider_ids,
            retrieval_reservation_ids=retrieval_ids,
            provider_call_count=len(provider_ids),
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
                    objective="Return the audited Researcher internal Revision result",
                    payload=result.model_dump(mode="json"),
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
        self.revision_exchange.create_internal_result_once(
            request_message,
            result_message,
        )
        if not any(
            item.message_id == result_message.message_id
            for item in state.message_history
        ):
            state.message_history.append(result_message)
        self._record_common_revision_audit(
            state,
            RevisionAuditEvent(
                audit_event_id=f"result_written_{request.revision_epoch}",
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                layer=LayerId.RESEARCHER,
                event_type=RevisionAuditEventType.RESULT_WRITTEN,
                actor_id=self.agent_id,
                message_id=result_message.message_id,
                artifact_ids=[report_id],
                reservation_ids=[*provider_ids, *retrieval_ids],
                reason=review.reason,
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

    def _record_common_revision_audit(
        self,
        state: ResearcherWorkflowState,
        event: RevisionAuditEvent,
    ) -> None:
        self.revision_exchange.create_audit_event_once(event)
        if event.audit_event_id not in state.revision_control.audit_event_ids:
            state.revision_control.audit_event_ids.append(event.audit_event_id)

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
            str(raw_result.get("task_id"))
            for results in state.agent_results.values()
            for raw_result in results
            if isinstance(raw_result.get("task_id"), str)
            and raw_result.get("task_id")
        }

    def _restore_results_from_message_history(
        self,
        state: ResearcherWorkflowState,
        *,
        persist: bool,
    ) -> int:
        """Reconcile durable RESULT PMP records before considering another call."""

        tasks = {
            task.task_id: task
            for task in (
                ResearchTask.model_validate(raw_task) for raw_task in state.research_tasks
            )
        }
        completed = self._completed_research_task_ids(state)
        restored = 0
        for message in state.message_history:
            if message.message_type not in {
                MessageType.RESULT.value,
                MessageType.RESEARCH_REVISION_RESULT.value,
            }:
                continue
            try:
                result = ResearchResult.model_validate(message.payload)
            except Exception:
                continue
            task = tasks.get(result.task_id)
            if (
                task is None
                or result.task_id in completed
                or result.agent_id != task.target_agent_id
                or message.sender_agent_id != task.target_agent_id
                or message.receiver_agent_id != self.agent_id
            ):
                continue
            state.agent_results.setdefault(task.target_agent_id, []).append(
                result.model_dump(mode="json")
            )
            if task.target_agent_id not in state.completed_agents:
                state.completed_agents.append(task.target_agent_id)
            completed.add(result.task_id)
            restored += 1
        if restored and persist:
            self.repository.save(state)
        return restored

    @staticmethod
    def _latest_initial_task_error(
        state: ResearcherWorkflowState,
        task: ResearchTask,
    ) -> PMPMessage | None:
        request_ids = {
            message.message_id
            for message in state.message_history
            if message.message_type == MessageType.TASK.value
            and message.receiver_agent_id == task.target_agent_id
            and message.payload.get("task_id") == task.task_id
            and not message.metadata.extensions.get("provider_task_id")
        }
        matches = [
            message
            for message in state.message_history
            if message.message_type == MessageType.ERROR.value
            and message.sender_agent_id == task.target_agent_id
            and message.receiver_agent_id == "researcher.manager"
            and message.parent_message_id in request_ids
            and message.payload.get("task_id") == task.task_id
        ]
        return matches[-1] if matches else None

    @staticmethod
    def _latest_runtime_model_repair_error(
        state: ResearcherWorkflowState,
        task: ResearchTask,
        repair_task_id: str | None,
    ) -> PMPMessage | None:
        if repair_task_id is None:
            return None
        request_ids = {
            message.message_id
            for message in state.message_history
            if message.message_type == MessageType.TASK.value
            and message.receiver_agent_id == task.target_agent_id
            and message.payload.get("task_id") == task.task_id
            and message.metadata.extensions.get("provider_task_id")
            == repair_task_id
        }
        matches = [
            message
            for message in state.message_history
            if message.message_type == MessageType.ERROR.value
            and message.sender_agent_id == task.target_agent_id
            and message.receiver_agent_id == "researcher.manager"
            and message.parent_message_id in request_ids
            and message.payload.get("task_id") == repair_task_id
        ]
        return matches[-1] if matches else None

    def _saved_retrieval_evidence(
        self,
        state: ResearcherWorkflowState,
        task: ResearchTask,
    ) -> SavedRetrievalEvidence | None:
        agent = self.registry.get(task.target_agent_id)
        coordinator = agent.retrieval_coordinator
        if coordinator is None:
            return None
        strategy = RetrievalStrategy(task.research_target)
        retrieval_id = coordinator._retrieval_id(
            state.workflow_id,
            task.task_id,
            task.target_agent_id,
            strategy.value,
        )
        path = coordinator._context_path(state.workflow_id, retrieval_id)
        original = self._load_saved_retrieval(
            path=path,
            retrieval_id=retrieval_id,
            workflow_id=state.workflow_id,
            retrieval_task_id=task.task_id,
            research_question_id=task.research_question_id,
            agent_id=task.target_agent_id,
            strategy=strategy.value,
            source="original",
        )
        if original is not None:
            return original

        retrieval_provider_id = getattr(coordinator.provider, "provider_id", None)
        if not isinstance(retrieval_provider_id, str):
            return None
        authorization = self.retrieval_reconstruction_store.for_original_task(
            workflow_id=state.workflow_id,
            retrieval_provider_id=retrieval_provider_id,
            original_task_id=task.task_id,
        )
        if (
            authorization is None
            or authorization.status != RetrievalReconstructionStatus.CONSUMED.value
            or authorization.retrieval_context_sha256 is None
            or authorization.agent_id != task.target_agent_id
            or authorization.runtime_model_id != agent.model
            or authorization.research_question_id != task.research_question_id
            or authorization.retrieval_strategy != strategy.value
        ):
            return None
        reconstructed_path = coordinator._context_path(
            state.workflow_id,
            authorization.retrieval_id,
        )
        reconstructed = self._load_saved_retrieval(
            path=reconstructed_path,
            retrieval_id=authorization.retrieval_id,
            workflow_id=state.workflow_id,
            retrieval_task_id=authorization.reconstruction_task_id,
            research_question_id=task.research_question_id,
            agent_id=task.target_agent_id,
            strategy=strategy.value,
            source="reconstruction",
        )
        if (
            reconstructed is None
            or reconstructed.sha256 != authorization.retrieval_context_sha256
        ):
            return None
        return reconstructed

    @staticmethod
    def _load_saved_retrieval(
        *,
        path,
        retrieval_id: str,
        workflow_id: str,
        retrieval_task_id: str,
        research_question_id: str | None,
        agent_id: str,
        strategy: str,
        source: str,
    ) -> SavedRetrievalEvidence | None:
        if not path.exists():
            return None
        try:
            context = RetrievedContext.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception:
            return None
        if (
            context.retrieval_id != retrieval_id
            or context.workflow_id != workflow_id
            or context.task_id != retrieval_task_id
            or context.research_question_id != research_question_id
            or context.agent_id != agent_id
            or context.retrieval_strategy != strategy
        ):
            return None
        return SavedRetrievalEvidence(
            context=context,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            retrieval_task_id=retrieval_task_id,
            source=source,
        )

    async def _execute_runtime_model_repair_task(
        self,
        state: ResearcherWorkflowState,
        task: ResearchTask,
        *,
        repair_task_id: str,
        retrieval_task_id: str,
        runtime_model: str,
        progress_callback: ProgressCallback | None,
        index: int,
        total: int,
    ) -> bool:
        state.status = WorkflowStatus.DISPATCHING
        state.current_agent_ids = [task.target_agent_id]
        request = self._create_task_message(
            state,
            task,
            is_revision=False,
            provider_task_id=repair_task_id,
            retrieval_task_id=retrieval_task_id,
        )
        state.message_history.append(request)
        self.repository.save(state)
        state.status = WorkflowStatus.COLLECTING
        self.repository.save(state)
        response = await self.registry.get(task.target_agent_id).execute(
            request,
            model_override=runtime_model,
        )
        state.message_history.append(response)
        error = self._validate_specialist_response(
            task,
            request,
            response,
            is_revision=False,
        )
        if error:
            self._record_failure(state, task, error)
            succeeded = False
        else:
            result = ResearchResult.model_validate(response.payload)
            if task.task_id not in self._completed_research_task_ids(state):
                state.agent_results.setdefault(task.target_agent_id, []).append(
                    result.model_dump(mode="json")
                )
            if task.target_agent_id not in state.completed_agents:
                state.completed_agents.append(task.target_agent_id)
            remaining_for_agent = {
                ResearchTask.model_validate(raw_task).task_id
                for raw_task in state.research_tasks
                if raw_task.get("target_agent_id") == task.target_agent_id
            } - self._completed_research_task_ids(state)
            if not remaining_for_agent and task.target_agent_id in state.failed_agents:
                state.failed_agents.remove(task.target_agent_id)
            succeeded = True
        state.current_agent_ids = []
        state.status = WorkflowStatus.RUNNING if succeeded else WorkflowStatus.FAILED
        # The complete PMP and validated result are committed before the next
        # task is authorized or invoked.
        self.repository.save(state)
        await self._emit(
            progress_callback,
            f"[{index}/{total}] runtime model repair {task.task_id}: "
            + ("completed" if succeeded else "failed"),
        )
        return succeeded

    @staticmethod
    def _validate_external_checkpoint(
        state: ResearcherWorkflowState,
        revision_tasks: list[ResearchTask],
        completed_task_ids: set[str],
    ) -> None:
        checkpoint = state.external_revision_status
        results_required = {
            ExternalRevisionCheckpoint.RESULTS_COLLECTED.value,
            ExternalRevisionCheckpoint.RESEARCH_RESULTS_COLLECTED.value,
            ExternalRevisionCheckpoint.REPORT_INTEGRATING.value,
            ExternalRevisionCheckpoint.REPORT_INTEGRATED.value,
            ExternalRevisionCheckpoint.QUALITY_REVIEWING.value,
            ExternalRevisionCheckpoint.QUALITY_REVIEWED.value,
            ExternalRevisionCheckpoint.REPLY_READY.value,
            ExternalRevisionCheckpoint.COMPLETED_REVISION.value,
        }
        report_required = {
            ExternalRevisionCheckpoint.REPORT_INTEGRATED.value,
            ExternalRevisionCheckpoint.QUALITY_REVIEWING.value,
            ExternalRevisionCheckpoint.QUALITY_REVIEWED.value,
            ExternalRevisionCheckpoint.REPLY_READY.value,
            ExternalRevisionCheckpoint.COMPLETED_REVISION.value,
        }
        if checkpoint in results_required:
            if state.external_revision_count < 1:
                raise ValueError("External revision checkpoint has no revision record")
            missing = [
                task.task_id
                for task in revision_tasks
                if task.task_id not in completed_task_ids
            ]
            if missing:
                raise ValueError(
                    "External revision checkpoint claims collected results but tasks are missing: "
                    + ", ".join(missing)
                )
            failed_targets = set(state.failed_agents) & {
                task.target_agent_id for task in revision_tasks
            }
            if failed_targets:
                raise ValueError(
                    "External revision checkpoint conflicts with failed agents: "
                    + ", ".join(sorted(failed_targets))
                )
        if checkpoint in report_required and state.research_report is None:
            raise ValueError("External revision checkpoint requires a Research Report")

    def _reconcile_written_external_reply(
        self,
        state: ResearcherWorkflowState,
        request: PMPMessage,
        request_ids: list[str],
    ) -> bool:
        try:
            reply = self.repository.load_deliberation_outbox(state.workflow_id)
        except FileNotFoundError:
            return False
        if (
            reply.message_type != MessageType.RESEARCH_REVISION_RESULT.value
            or reply.parent_message_id != request.message_id
            or reply.sender_agent_id != self.agent_id
            or reply.receiver_agent_id != "deliberation.manager"
            or set(reply.payload.get("resolved_revision_request_ids") or [])
            != set(request_ids)
        ):
            return False
        self.pmp_validator.validate(reply)
        self._validate_deliberation_handoff(reply.payload)
        if not any(message.message_id == reply.message_id for message in state.message_history):
            state.message_history.append(reply)
        matching_record = next(
            (
                record
                for record in reversed(state.external_revision_history)
                if record.parent_message_id == request.message_id
            ),
            None,
        )
        if matching_record is not None:
            matching_record.status = "reply_sent"
            matching_record.completed_at = matching_record.completed_at or utc_now()
            matching_record.reply_message_id = reply.message_id
        state.external_revision_reply_sent = True
        state.external_revision_status = ExternalRevisionCheckpoint.COMPLETED_REVISION
        state.pending_external_revision_request_ids = []
        state.pending_revision_parent_message_id = None
        state.pending_revision_source_agent_id = None
        state.status = WorkflowStatus.COMPLETED_REVISION
        state.error = None
        state.current_agent_ids = []
        state.completed_at = state.completed_at or utc_now()
        return True

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
        provider_task_id: str | None = None,
        retrieval_task_id: str | None = None,
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
                "return_only_assigned_source_type": task.research_target,
                "cross_category_sources_are_invalid": True,
                "placeholder_or_example_invalid_sources_are_forbidden": True,
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
        provider_id = getattr(
            self.registry.get(task.target_agent_id).provider,
            "provider_id",
            None,
        )
        if provider_id != "mock":
            placeholder_ids = [
                source.source_id
                for source in result.sources
                if self._is_placeholder_source(source)
            ]
            if placeholder_ids:
                return (
                    f"{task.target_agent_id} returned placeholder sources outside the "
                    f"Mock provider: {', '.join(placeholder_ids)}"
                )
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
        reuse_external_report: bool = False,
    ) -> ResearcherWorkflowState:
        while True:
            state.status = WorkflowStatus.INTEGRATING
            if external_request is not None and reuse_external_report:
                if state.research_report is None:
                    raise ValueError(
                        "External revision checkpoint requires a saved Research Report"
                    )
                report = ResearchReport.model_validate(state.research_report)
                self._validate_source_metadata_contracts(report.sources)
                reuse_external_report = False
                await self._emit(
                    progress_callback,
                    "Existing integrated Research Report detected; rebuild skipped",
                )
            else:
                if external_request is not None:
                    state.external_revision_status = (
                        ExternalRevisionCheckpoint.REPORT_INTEGRATING
                    )
                    self.repository.save(state)
                report = self._build_report(state)
            if external_request is not None:
                state.external_revision_status = (
                    ExternalRevisionCheckpoint.REPORT_INTEGRATED
                )
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
            if (
                state.human_evidence_decision is not None
                and state.human_evidence_decision.quality_review_id
                != review_response.message_id
            ):
                state.human_evidence_decision = None
                state.accepted_evidence_gaps = []
                state.evidence_revision_plan = None
            state.review_result = review_summary
            if external_request is not None:
                state.external_revision_status = (
                    ExternalRevisionCheckpoint.QUALITY_REVIEWED
                )
            self.repository.save(state)

            assessed_report = review.approved_research_report or report
            assessed_report.review = ResearchReportReview.model_validate(review_summary)
            state.research_report = assessed_report.model_dump(mode="json")
            self.repository.save_report(assessed_report)
            if state.revision_control.phase == RevisionControlPhase.EXECUTING.value:
                self._finalize_common_internal_revision(
                    state,
                    review_response=review_response,
                    review=review,
                )
            self.repository.save(state)
            await self._emit(
                progress_callback,
                f"Quality Reviewer assessment: {review.status}",
            )

            upstream_findings = self._upstream_plan_findings(review)
            if upstream_findings:
                request = self._route_producer_revision(
                    state,
                    review=review,
                    review_message=review_response,
                )
                await self._emit(
                    progress_callback,
                    "Researcher Quality Review requested an upstream Research Plan "
                    f"Revision ({request.revision_request_id})",
                )
                return state

            summary = self._prepare_human_evidence_gate(state)
            if external_request is not None:
                state.external_revision_history[-1].status = "blocked"
                self.repository.save(state)
            if summary.eligible:
                await self._emit(
                    progress_callback,
                    "Human Evidence Gate: explicit ACCEPT, "
                    "ACCEPT_WITH_LIMITATIONS, or REVISE decision required",
                )
            else:
                await self._emit(
                    progress_callback,
                    "Human Evidence Gate closed: unresolved Hard Integrity Failure",
                )
            return state

    async def _request_review(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        *,
        external_revision: ExternalResearchRevisionRecord | None = None,
    ) -> tuple[ResearchQualityReviewOutput, PMPMessage]:
        state.status = WorkflowStatus.REVIEWING
        base_review_task_id = self._quality_review_task_id(state, external_revision)
        contract_review_task_id = f"{base_review_task_id}_contract_v2"
        normal_review_task_id = (
            contract_review_task_id
            if self._requires_quality_review_contract_repair(
                state,
                base_review_task_id,
                contract_review_task_id,
            )
            else base_review_task_id
        )
        reviewer = self.registry.get(QUALITY_REVIEWER_ID)
        provider_id = getattr(reviewer.provider, "provider_id", None)
        authorization = (
            self.provider_retry_store.for_original_task(
                workflow_id=state.workflow_id,
                provider_id=provider_id,
                original_task_id=normal_review_task_id,
            )
            if isinstance(provider_id, str)
            else None
        )
        retry_task_id = authorization.retry_task_id if authorization is not None else None
        saved_task_ids = list(
            dict.fromkeys(
                task_id
                for task_id in (
                    retry_task_id,
                    contract_review_task_id,
                    base_review_task_id,
                )
                if task_id is not None
            )
        )
        for saved_task_id in saved_task_ids:
            saved_exchange = self._saved_quality_review_exchange(state, saved_task_id)
            if saved_exchange is not None:
                return saved_exchange
        review_task_id = retry_task_id or normal_review_task_id
        request = PMPMessage.create(
            workflow_id=state.workflow_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=QUALITY_REVIEWER_ID,
            message_type=MessageType.TASK,
            objective="Review Research Report against the approved Research Plan",
            payload={
                "task_id": review_task_id,
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

    def _saved_quality_review_exchange(
        self,
        state: ResearcherWorkflowState,
        review_task_id: str,
    ) -> tuple[ResearchQualityReviewOutput, PMPMessage] | None:
        requests = [
            message
            for message in state.message_history
            if message.sender_agent_id == self.agent_id
            and message.receiver_agent_id == QUALITY_REVIEWER_ID
            and message.message_type == MessageType.TASK.value
            and message.payload.get("task_id") == review_task_id
        ]
        for request in reversed(requests):
            responses = [
                message
                for message in reversed(state.message_history)
                if message.parent_message_id == request.message_id
                and message.sender_agent_id == QUALITY_REVIEWER_ID
                and message.receiver_agent_id == self.agent_id
            ]
            for response in responses:
                if response.message_type == MessageType.REVIEW.value:
                    error = self._validate_review_response(request, response)
                    if error is None:
                        return ResearchQualityReviewOutput.model_validate(response.payload), response
                if response.message_type != MessageType.ERROR.value:
                    continue
                invalid_payload = response.payload.get("invalid_payload")
                if not isinstance(invalid_payload, dict):
                    continue
                try:
                    review = ResearchQualityReviewOutput.model_validate(invalid_payload)
                except Exception:
                    continue
                recovered_response = PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=request.message_id,
                    sender_agent_id=QUALITY_REVIEWER_ID,
                    receiver_agent_id=self.agent_id,
                    message_type=MessageType.REVIEW,
                    objective="Recover previously returned Quality Review after contract validation",
                    payload=review.model_dump(mode="json"),
                    constraints=request.constraints,
                    context=PMPContext(
                        current_stage=QUALITY_REVIEWER_ID,
                        previous_stage=request.context.current_stage,
                        next_stage=self.agent_id,
                    ),
                    metadata=PMPMetadata(
                        status=MessageStatus.COMPLETED,
                        retry_count=response.metadata.retry_count,
                        notes="Recovered from persisted invalid_payload without a provider call",
                        extensions=response.metadata.extensions,
                    ),
                )
                self.pmp_validator.validate(recovered_response)
                state.message_history.append(recovered_response)
                self.repository.save(state)
                return review, recovered_response
        return None

    @staticmethod
    def _requires_quality_review_contract_repair(
        state: ResearcherWorkflowState,
        base_review_task_id: str,
        contract_review_task_id: str,
    ) -> bool:
        requests = [
            message
            for message in state.message_history
            if message.sender_agent_id == "researcher.manager"
            and message.receiver_agent_id == QUALITY_REVIEWER_ID
            and message.message_type == MessageType.TASK.value
        ]
        if any(
            message.payload.get("task_id") == contract_review_task_id
            for message in requests
        ):
            return True
        for request in reversed(requests):
            if request.payload.get("task_id") != base_review_task_id:
                continue
            error_response = next(
                (
                    message
                    for message in reversed(state.message_history)
                    if message.parent_message_id == request.message_id
                    and message.sender_agent_id == QUALITY_REVIEWER_ID
                    and message.receiver_agent_id == "researcher.manager"
                    and message.message_type == MessageType.ERROR.value
                ),
                None,
            )
            return bool(
                error_response is not None
                and error_response.payload.get("error_class") == "PayloadValidationError"
                and not isinstance(error_response.payload.get("invalid_payload"), dict)
            )
        return False

    @staticmethod
    def _quality_review_task_id(
        state: ResearcherWorkflowState,
        external_revision: ExternalResearchRevisionRecord | None,
    ) -> str:
        if external_revision is not None:
            if state.revision_count > 0:
                return (
                    f"research_quality_review_external_{external_revision.iteration}_"
                    f"internal_{state.revision_count}"
                )
            return f"research_quality_review_external_{external_revision.iteration}"
        if state.revision_count > 0:
            return f"research_quality_review_internal_{state.revision_count}"
        return "research_quality_review_initial"

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
        *,
        revision_targets: list[str] | None = None,
    ) -> list[ResearchTask]:
        originals = [ResearchTask.model_validate(item) for item in state.research_tasks]
        selected: dict[tuple[str, str], ResearchTask] = {}
        for finding in review.findings:
            if finding.target_agent_id in {None, ResearcherManager.agent_id}:
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
                data["task_id"] = ResearcherManager._revision_task_id(
                    state,
                    original,
                )
                data["revision_context"] = {
                    "revision_request_id": state.revision_control.active_request_id,
                    "finding_id": finding.finding_id,
                    "issue": finding.issue,
                    "required_action": finding.required_action,
                    "revision_iteration": state.revision_count,
                    "external_revision_iteration": state.external_revision_count,
                    "explicit_operator_revision": True,
                }
                selected[(original.target_agent_id, original.research_question_id)] = (
                    ResearchTask.model_validate(data)
                )
        for target in revision_targets or review.revision_targets:
            if any(key[0] == target for key in selected):
                continue
            matches = [task for task in originals if task.target_agent_id == target]
            if not matches:
                raise ValueError(
                    f"Quality Reviewer requested an out-of-plan revision target: {target}"
                )
            for original in matches:
                data = original.model_dump(mode="json")
                data["task_id"] = ResearcherManager._revision_task_id(
                    state,
                    original,
                )
                data["revision_context"] = {
                    "revision_request_id": state.revision_control.active_request_id,
                    "issue": review.reason,
                    "required_action": "Resolve Quality Reviewer findings",
                    "revision_iteration": state.revision_count,
                    "external_revision_iteration": state.external_revision_count,
                    "explicit_operator_revision": True,
                }
                selected[(original.target_agent_id, original.research_question_id)] = (
                    ResearchTask.model_validate(data)
                )
        if not selected:
            raise ValueError("revision_required review did not resolve to any Research Task")
        return list(selected.values())

    @staticmethod
    def _revision_task_id(
        state: ResearcherWorkflowState,
        original: ResearchTask,
    ) -> str:
        agent_name = original.target_agent_id.split(".", 1)[-1]
        question = "".join(
            character
            for character in original.research_question_id
            if character.isalnum() or character in {"_", "-"}
        )[:32]
        return (
            f"research_revision_external_{state.external_revision_count}_"
            f"internal_{state.revision_count}_{agent_name}_{question}"
        )

    def _active_provider_id(self) -> str | None:
        provider_id = getattr(
            self.registry.get(QUALITY_REVIEWER_ID).provider,
            "provider_id",
            None,
        )
        return provider_id if isinstance(provider_id, str) else None

    def _result_matches_active_provider(
        self,
        state: ResearcherWorkflowState,
        task_id: str,
    ) -> bool:
        active_provider_id = self._active_provider_id()
        reservation_root = self.repository.data_dir / "provider_call_reservations"
        if active_provider_id is None or not reservation_root.exists():
            return True
        matching_provider_ids = {
            provider_directory.name
            for provider_directory in reservation_root.iterdir()
            if provider_directory.is_dir()
            and self.provider_retry_store.reservation_path(
                provider_id=provider_directory.name,
                workflow_id=state.workflow_id,
                task_id=task_id,
            ).exists()
        }
        return not matching_provider_ids or active_provider_id in matching_provider_ids

    @staticmethod
    def _source_type_for_agent(agent_id: str) -> str:
        try:
            return next(
                target.value
                for target, mapped_agent_id in RESEARCH_TARGET_MAP.items()
                if mapped_agent_id == agent_id
            )
        except StopIteration as exc:
            raise ValueError(f"Unknown Researcher agent in saved results: {agent_id}") from exc

    @staticmethod
    def _is_placeholder_source(source: ResearchSource) -> bool:
        host = (urlsplit(str(source.url)).hostname or "").lower()
        if host == "example.invalid" or host.endswith(".invalid"):
            return True
        identity_values = [
            source.title,
            source.source_name,
            source.author_or_organization or "",
            *(
                value
                for value in source.source_specific_metadata.values()
                if isinstance(value, str)
            ),
        ]
        if any("mock" in value.casefold() for value in identity_values):
            return True
        doi = str(source.source_specific_metadata.get("doi") or "").lower()
        return doi.startswith("10.0000/mock")

    def _build_report(self, state: ResearcherWorkflowState) -> ResearchReport:
        plan = ResearchPlan.model_validate(state.research_plan)
        raw_sources: list[ResearchSource] = []
        result_limitations: list[str] = []
        excluded_provider_results = 0
        excluded_category_sources = 0
        excluded_invalid_sources = 0
        for agent_id, raw_results in state.agent_results.items():
            for raw_result in raw_results:
                task_id = raw_result.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    excluded_invalid_sources += len(raw_result.get("sources") or [])
                    continue
                if not self._result_matches_active_provider(state, task_id):
                    excluded_provider_results += 1
                    continue
                expected_source_type = self._source_type_for_agent(agent_id)
                accepted_source_count = 0
                for raw_source in raw_result.get("sources") or []:
                    try:
                        source = ResearchSource.model_validate(
                            canonicalize_legacy_trace_ids(raw_source)
                        )
                    except Exception:
                        excluded_invalid_sources += 1
                        continue
                    source, recognized_media_reclassified = (
                        self._canonicalize_recognized_media_source(source)
                    )
                    if (
                        source.source_type != expected_source_type
                        and not (
                            recognized_media_reclassified
                            and expected_source_type
                            == ResearchSourceType.EXPERT.value
                            and source.source_type == ResearchSourceType.NEWS.value
                        )
                    ):
                        excluded_category_sources += 1
                        continue
                    if self._active_provider_id() != "mock" and self._is_placeholder_source(
                        source
                    ):
                        excluded_invalid_sources += 1
                        continue
                    raw_sources.append(source)
                    accepted_source_count += 1
                if (
                    accepted_source_count > 0
                    or raw_result.get("coverage_status") == CoverageStatus.NO_RESULT.value
                ):
                    result_limitations.extend(
                        str(limitation)
                        for limitation in raw_result.get("limitations") or []
                        if str(limitation).strip()
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

        provenance_limitations: list[str] = []
        if excluded_provider_results:
            provenance_limitations.append(
                "Provider provenance filtering excluded "
                f"{excluded_provider_results} saved ResearchResult record(s) from a "
                "different provider namespace."
            )
        if excluded_category_sources:
            provenance_limitations.append(
                "Researcher category validation excluded "
                f"{excluded_category_sources} source record(s) returned outside the "
                "assigned source type."
            )
        if excluded_invalid_sources:
            provenance_limitations.append(
                "Source validation excluded "
                f"{excluded_invalid_sources} invalid or placeholder source record(s)."
            )
        limitations = self._dedupe_report_limitations(
            [item.get("message", str(item)) for item in state.limitations]
            + result_limitations
            + provenance_limitations,
            sources,
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
            elif len(related) >= 2:
                categories = sorted({str(source.source_type) for source in related})
                observations.append(
                    CrossSourceObservation(
                        observation_id=new_id("obs"),
                        description=(
                            f"{coverage.research_question_id} is documented across "
                            f"the collected {', '.join(categories)} source scopes; "
                            "the sources are retained separately without a truth judgment."
                        ),
                        supporting_evidence_ids=[
                            source.evidence_id for source in related
                        ],
                        observation_type=ObservationType.SCOPE_DIFFERENCE,
                        limitations=[
                            "Different source categories are not interchangeable and "
                            "must be interpreted by Deliberation."
                        ],
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
    ) -> PMPMessage:
        decision = state.human_evidence_decision
        if decision is None:
            raise ValueError("Researcher handoff requires a Human Evidence Decision")
        try:
            existing = self.repository.load_deliberation_outbox(state.workflow_id)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.message_type != MessageType.RESEARCH_RESULT.value
                or (existing.payload.get("human_evidence_decision") or {}).get(
                    "decision_id"
                )
                != decision.decision_id
            ):
                raise ValueError("Existing Deliberation outbox has a different decision")
            if not any(
                item.message_id == existing.message_id for item in state.message_history
            ):
                state.message_history.append(existing)
            return existing
        report_payload = report.model_dump(mode="json")
        payload = {
            **report_payload,
            "research_report": report_payload,
            "quality_review": state.review_result or {},
            "known_limitations": report.research_limitations,
            "unresolved_gaps": [gap.model_dump(mode="json") for gap in report.evidence_gaps],
            "human_evidence_decision": decision.model_dump(mode="json"),
            "accepted_evidence_gaps": [
                item.model_dump(mode="json") for item in state.accepted_evidence_gaps
            ],
            "human_evidence_integrity_repairs": [
                item.model_dump(mode="json")
                for item in state.human_evidence_integrity_repairs
            ],
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
        message.message_id = str(
            uuid5(
                NAMESPACE_URL,
                f"prdcp:{state.workflow_id}:researcher-handoff:{decision.decision_id}",
            )
        )
        self.pmp_validator.validate(message)
        self.repository.save_deliberation_outbox(message)
        state.message_history.append(message)
        return message

    def _send_revision_result_to_deliberation(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
        original_request: PMPMessage,
    ) -> PMPMessage:
        decision = state.human_evidence_decision
        if decision is None:
            raise ValueError("Researcher revision handoff requires a Human Evidence Decision")
        try:
            existing = self.repository.load_deliberation_outbox(state.workflow_id)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            existing.message_type == MessageType.RESEARCH_REVISION_RESULT.value
        ):
            if (existing.payload.get("human_evidence_decision") or {}).get(
                "decision_id"
            ) == decision.decision_id:
                if not any(
                    item.message_id == existing.message_id for item in state.message_history
                ):
                    state.message_history.append(existing)
                self._write_canonical_deliberation_revision_result(
                    state,
                    report,
                )
                return existing
            # A later external revision cycle has a different Quality Review and
            # therefore a different Human Decision. The single-slot outbox may
            # advance to that newer correlated reply; the earlier reply remains
            # immutable in message_history.
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
            "human_evidence_decision": decision.model_dump(mode="json"),
            "accepted_evidence_gaps": [
                item.model_dump(mode="json") for item in state.accepted_evidence_gaps
            ],
            "human_evidence_integrity_repairs": [
                item.model_dump(mode="json")
                for item in state.human_evidence_integrity_repairs
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
        message.message_id = str(
            uuid5(
                NAMESPACE_URL,
                f"prdcp:{state.workflow_id}:researcher-revision-handoff:"
                f"{decision.decision_id}",
            )
        )
        self.pmp_validator.validate(message)
        self.repository.save_deliberation_outbox(message)
        state.message_history.append(message)
        self._write_canonical_deliberation_revision_result(state, report)
        return message

    def _write_canonical_deliberation_revision_result(
        self,
        state: ResearcherWorkflowState,
        report: ResearchReport,
    ) -> PMPMessage | None:
        if not state.pending_external_revision_request_ids:
            return None
        request_message = self._canonical_deliberation_revision_request(
            state,
            request_ids=state.pending_external_revision_request_ids,
        )
        if request_message is None:
            return None
        request = RevisionRequestV1.model_validate(request_message.payload)
        try:
            existing = self.revision_exchange.load_result(
                requester_layer=LayerId.DELIBERATION,
                workflow_id=state.workflow_id,
                revision_request_id=request.revision_request_id,
                request_message=request_message,
            )
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if not any(
                item.message_id == existing.message_id
                for item in state.message_history
            ):
                state.message_history.append(existing)
            return existing

        decision = state.human_evidence_decision
        if decision is None:
            raise ValueError("Canonical Researcher Revision Result requires Human Decision")
        completed = decision.decision == HumanEvidenceDecisionType.ACCEPT.value
        report_payload = report.model_dump(mode="json")
        result_artifact = RevisionArtifactRef(
            artifact_type="researcher.research_report",
            artifact_id=report.research_report_id,
            sha256=canonical_sha256(report_payload),
        )
        external_ids = set(state.pending_external_revision_request_ids)
        provider_ids = list(
            dict.fromkeys(
                str(message.payload.get("task_id"))
                for message in state.message_history
                if message.sender_agent_id == self.agent_id
                and message.message_type == MessageType.TASK.value
                and isinstance(message.payload.get("task_id"), str)
                and (
                    (message.payload.get("revision_context") or {}).get(
                        "revision_request_id"
                    )
                    in external_ids
                    or message.receiver_agent_id == QUALITY_REVIEWER_ID
                )
            )
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
            status=(
                RevisionExecutionStatus.COMPLETED
                if completed
                else RevisionExecutionStatus.PARTIAL
            ),
            base_artifacts=request.base_artifacts,
            result_artifacts=[result_artifact],
            finding_dispositions=[
                RevisionFindingDisposition(
                    finding_id=finding_id,
                    outcome=(
                        RevisionFindingOutcome.RESOLVED
                        if completed
                        else RevisionFindingOutcome.UNRESOLVED
                    ),
                    reason=decision.reason,
                    result_artifact_ids=[report.research_report_id],
                )
                for finding_id in request.source_finding_ids
            ],
            human_selection_impact=HumanSelectionImpact.NOT_APPLICABLE,
            provider_reservation_ids=provider_ids,
            retrieval_reservation_ids=[],
            provider_call_count=len(provider_ids),
            retrieval_call_count=0,
        )
        message_id = str(
            uuid5(NAMESPACE_URL, f"{request.revision_request_id}:result-message")
        )
        message = PMPMessage.model_validate(
            {
                **PMPMessage.create(
                    workflow_id=state.workflow_id,
                    parent_message_id=request_message.message_id,
                    sender_agent_id=self.agent_id,
                    receiver_agent_id="deliberation.manager",
                    message_type=MessageType.REVISION_RESULT,
                    objective="Return the audited Researcher upstream Revision result",
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
                "message_id": message_id,
            }
        )
        self.revision_exchange.create_result_once(request_message, message)
        if not any(item.message_id == message.message_id for item in state.message_history):
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
            "human_evidence_decision",
            "accepted_evidence_gaps",
        }
        missing = sorted(required - payload.keys())
        if missing:
            raise ValueError(
                f"Researcher→Deliberation handoff is missing: {', '.join(missing)}"
            )
        decision = HumanEvidenceDecision.model_validate(
            payload["human_evidence_decision"]
        )
        if decision.decision not in {
            HumanEvidenceDecisionType.ACCEPT.value,
            HumanEvidenceDecisionType.ACCEPT_WITH_LIMITATIONS.value,
        }:
            raise ValueError("Researcher handoff decision does not permit Deliberation")
        accepted = [
            AcceptedEvidenceGap.model_validate(item)
            for item in payload["accepted_evidence_gaps"]
        ]
        if {
            item.finding_id for item in accepted
        } != set(decision.accepted_finding_ids):
            raise ValueError("Accepted Evidence Gap IDs do not match the Human Decision")
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
