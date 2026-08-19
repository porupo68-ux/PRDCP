from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from abc import ABC
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from common.models.errors import (
    AgentExecutionError,
    NonRetryableAgentError,
    PayloadValidationError,
    RetryableAgentError,
)
from common.models.pmp import MessageStatus, MessageType, PMPContext, PMPMessage, PMPMetadata
from common.provider_capability_repair import (
    PROVIDER_CAPABILITY_REPAIR_SUFFIX,
    ProviderCapabilityRepairAuthorizationStore,
)
from common.provider_contract_repair import (
    PROVIDER_CONTRACT_REPAIR_SUFFIX,
    ProviderContractRepairAuthorizationStore,
)
from common.provider_output_repair import (
    PROVIDER_OUTPUT_REPAIR_SUFFIX,
    ProviderOutputRepairAuthorizationStore,
)
from common.provider_retry import OPERATOR_RETRY_SUFFIX, ProviderRetryAuthorizationStore
from common.provider_runtime_model_repair import (
    RUNTIME_MODEL_REPAIR_SUFFIX,
    RuntimeModelRepairAuthorizationStore,
)
from common.provider_runtime_output_repair import (
    RUNTIME_ADAPTER_REPAIR_SUFFIX,
    RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX,
    RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX,
    RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX,
    RuntimeAdapterRepairAuthorizationStore,
    RuntimeIdentityHydrationRepairAuthorizationStore,
    RuntimeModelOutputRepairAuthorizationStore,
    RuntimeProvenanceHydrationRepairAuthorizationStore,
)
from common.role_definitions import RoleDefinitionLoader
from common.role_definitions.agent_runtime import (
    prepare_agent_execution,
    role_definition_extensions,
    specialize_agent_execution_prompt,
)
from common.role_definitions.models import RoleDefinitionSnapshot
from common.validation import PMPValidator, PayloadValidator
from config.settings import BASE_DIR

if TYPE_CHECKING:
    from providers.base import ModelProvider


def _provider_call_reservations_dir(provider: object | None = None) -> Path:
    configured_root = getattr(provider, "reservation_root", None)
    if configured_root is not None:
        return Path(configured_root)
    configured_data_dir = os.getenv("PRDCP_DATA_DIR", "").strip()
    data_dir = (
        Path(configured_data_dir) if configured_data_dir else BASE_DIR / "storage" / "data"
    )
    return data_dir / "provider_call_reservations"


class StructuredAgent(ABC):
    """Common RD-aware execution pipeline for every specialist agent.

    Layer-specific base classes only declare their prompt directory, manager,
    accepted PMP message types, and schemas. Validation, retries, RD snapshot
    handling, error conversion, and trace metadata stay identical across layers.
    """

    agent_id: ClassVar[str]
    input_schema: ClassVar[type[BaseModel]]
    output_schema: ClassVar[type[BaseModel]]
    output_message_type: ClassVar[MessageType] = MessageType.RESULT
    accepted_message_types: ClassVar[set[str]] = {
        MessageType.TASK.value,
        MessageType.INFO.value,
    }

    prompt_layer: ClassVar[str]
    manager_agent_id: ClassVar[str]
    result_objective_suffix: ClassVar[str] = "result"
    error_next_stage: ClassVar[str | None] = None
    use_request_previous_stage: ClassVar[bool] = False

    def __init__(
        self,
        provider: ModelProvider,
        payload_validator: PayloadValidator,
        pmp_validator: PMPValidator,
        *,
        model: str = "mock",
        rd_loader: RoleDefinitionLoader | None = None,
        max_technical_retries: int | None = None,
        demo_safe_mode: bool = True,
        retrieval_coordinator: object | None = None,
    ) -> None:
        self.provider = provider
        self.payload_validator = payload_validator
        self.pmp_validator = pmp_validator
        self.model = model
        # Kept only for compatibility with early prototypes. RD runtime_config is
        # authoritative, so callers cannot silently override the role contract.
        self.legacy_max_technical_retries = max_technical_retries
        self.demo_safe_mode = demo_safe_mode
        self.retrieval_coordinator = retrieval_coordinator
        self.rd_loader = rd_loader or RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=BASE_DIR / "storage" / "data" / "logs" / "rd_access.jsonl",
        )

    async def execute(
        self,
        message: PMPMessage,
        *,
        model_override: str | None = None,
    ) -> PMPMessage:
        snapshot: RoleDefinitionSnapshot | None = None
        effective_model = model_override or self.model
        try:
            validated = self.validate_message(message)
            result_message_type = self.resolve_result_message_type(validated)
            execution = prepare_agent_execution(
                loader=self.rd_loader,
                agent_id=self.agent_id,
                message=validated,
                agent_prompt=self.agent_prompt,
                output_schema=self.output_schema,
                expected_output_message_type=result_message_type.value,
            )
            snapshot = execution.snapshot
            payload = self.payload_validator.validate(validated.payload, self.input_schema)
            provider_id = getattr(self.provider, "provider_id", None)
            input_data = await self.prepare_provider_input(
                payload,
                message=validated,
                timeout_seconds=execution.runtime_config.timeout_seconds,
            )
            if input_data != validated.payload:
                execution = specialize_agent_execution_prompt(
                    execution,
                    loader=self.rd_loader,
                    agent_id=self.agent_id,
                    message=validated,
                    agent_prompt=self.agent_prompt,
                    output_schema=self.output_schema,
                    input_data=input_data,
                )
            explicit_task_id = self._explicit_logical_task_id(
                validated,
                input_data=input_data,
            )
            logical_task_id = explicit_task_id or self.agent_id
            preflight = getattr(self.provider, "validate_request_budget", None)
            if callable(preflight):
                preflight(
                    agent_id=self.agent_id,
                    model=effective_model,
                    system_prompt=execution.system_prompt,
                    input_data=input_data,
                    output_schema=self.output_schema,
                )
            if self.demo_safe_mode or (
                isinstance(provider_id, str) and explicit_task_id is not None
            ):
                self._reserve_provider_invocation(
                    validated,
                    logical_task_id=logical_task_id,
                    model_id=effective_model,
                )
            result, retry_count = await self.run(
                payload,
                system_prompt=execution.system_prompt,
                max_technical_retries=execution.runtime_config.technical_retry_limit,
                timeout_seconds=execution.runtime_config.timeout_seconds,
                model=effective_model,
                prepared_input=input_data,
            )
            return self.create_result_message(
                validated,
                result,
                retry_count,
                result_message_type=result_message_type,
                snapshot=snapshot,
            )
        except Exception as exc:
            return self.create_error_message(
                message,
                exc,
                snapshot=snapshot,
                model_id=effective_model,
            )

    def validate_message(self, message: PMPMessage) -> PMPMessage:
        validated = self.pmp_validator.validate(message)
        if validated.receiver_agent_id != self.agent_id:
            raise AgentExecutionError(
                f"{self.agent_id} cannot execute a message addressed to "
                f"{validated.receiver_agent_id}"
            )
        if validated.message_type not in self.accepted_message_types:
            raise AgentExecutionError(f"Unsupported message type: {validated.message_type}")
        return validated

    async def run(
        self,
        payload: BaseModel,
        *,
        system_prompt: str,
        max_technical_retries: int,
        timeout_seconds: int,
        model: str | None = None,
        prepared_input: dict | None = None,
    ) -> tuple[BaseModel, int]:
        input_data = (
            payload.model_dump(mode="json")
            if prepared_input is None
            else prepared_input
        )
        effective_model = model or self.model
        effective_retry_limit = (
            0 if self.demo_safe_mode else min(max(max_technical_retries, 0), 1)
        )
        for attempt in range(effective_retry_limit + 1):
            try:
                raw = await asyncio.wait_for(
                    self.provider.generate_structured(
                        model=effective_model,
                        system_prompt=system_prompt,
                        input_data=input_data,
                        output_schema=self.output_schema,
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
                normalized_raw = self.normalize_provider_output(
                    raw,
                    provider_input=input_data,
                )
                validated_result = self.payload_validator.validate(
                    normalized_raw,
                    self.output_schema,
                )
                return self.validate_output_contract(
                    payload,
                    validated_result,
                    provider_input=input_data,
                ), attempt
            except PayloadValidationError as exc:
                exc.retry_count = attempt
                raise
            except NonRetryableAgentError as exc:
                exc.retry_count = attempt
                raise
            except RetryableAgentError as exc:
                exc.retry_count = attempt
                if (
                    not exc.automatic_retry_allowed
                    or attempt >= effective_retry_limit
                ):
                    raise
            except TimeoutError as exc:
                timeout_error = RetryableAgentError(
                    f"{self.agent_id} provider call timed out after {timeout_seconds} seconds",
                    retry_count=attempt,
                    provider=type(self.provider).__name__,
                    model_id=effective_model,
                )
                if attempt >= effective_retry_limit:
                    raise timeout_error from exc
            except AgentExecutionError as exc:
                raise NonRetryableAgentError(
                    f"{self.agent_id} stopped on an unclassified provider error: {exc}",
                    http_status=exc.http_status,
                    retry_count=attempt,
                    provider=exc.provider or type(self.provider).__name__,
                    model_id=exc.model_id or effective_model,
                ) from exc
        raise RuntimeError("unreachable retry state")

    async def prepare_provider_input(
        self,
        payload: BaseModel,
        *,
        message: PMPMessage,
        timeout_seconds: int,
    ) -> dict:
        """Build the runtime-only view sent to the model.

        The default is deliberately a no-op so the canonical PMP payload stays
        authoritative for every agent that does not opt into retrieval.
        """

        del message, timeout_seconds
        return payload.model_dump(mode="json")

    def normalize_provider_output(
        self,
        raw: dict,
        *,
        provider_input: dict | None = None,
    ) -> dict:
        """Restore deterministic fields omitted from a provider-bound schema.

        Retrieval-aware agents may ask the model to select a compact source ID
        while materializing immutable source metadata from the persisted input.
        The default leaves ordinary agent output unchanged.
        """

        del provider_input
        return raw

    def validate_output_contract(
        self,
        input_payload: BaseModel,
        output_payload: BaseModel,
        *,
        provider_input: dict | None = None,
    ) -> BaseModel:
        """Apply an optional input-aware domain contract after schema validation.

        JSON Schema can validate the shape of an ID field, but it cannot prove
        that the ID belongs to the canonical set supplied in the request. Layer
        adapters may override this hook for those cross-payload invariants.
        """
        del provider_input
        return output_payload

    def _reserve_provider_invocation(
        self,
        message: PMPMessage,
        *,
        logical_task_id: str,
        model_id: str | None = None,
    ) -> None:
        provider_name = type(self.provider).__name__
        provider_id = getattr(self.provider, "provider_id", None)
        effective_model = model_id or self.model
        if not isinstance(provider_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}", provider_id
        ):
            raise NonRetryableAgentError(
                f"Provider invocation requires a stable logical provider ID for {self.agent_id}; "
                "provider call blocked",
                provider=provider_name,
                model_id=effective_model,
            )
        workflow_component = self._reservation_path_component(message.workflow_id)
        task_component = self._reservation_path_component(logical_task_id)
        reservation_path = (
            _provider_call_reservations_dir(self.provider)
            / provider_id
            / workflow_component
            / f"{task_component}.json"
        )
        reservation = {
            "workflow_id": message.workflow_id,
            "task_id": logical_task_id,
            "agent_id": self.agent_id,
            "provider": provider_name,
            "model_id": effective_model,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
        }
        retry_authorization = None
        retry_store = None
        repair_authorization = None
        repair_store = None
        capability_authorization = None
        capability_store = None
        output_repair_authorization = None
        output_repair_store = None
        runtime_model_repair_authorization = None
        runtime_model_repair_store = None
        runtime_output_repair_authorization = None
        runtime_output_repair_store = None
        runtime_adapter_repair_authorization = None
        runtime_adapter_repair_store = None
        runtime_identity_repair_authorization = None
        runtime_identity_repair_store = None
        runtime_provenance_repair_authorization = None
        runtime_provenance_repair_store = None
        if logical_task_id.endswith(OPERATOR_RETRY_SUFFIX):
            retry_store = ProviderRetryAuthorizationStore.from_reservation_root(
                _provider_call_reservations_dir(self.provider)
            )
            try:
                retry_authorization = retry_store.require_pending_retry(
                    workflow_id=message.workflow_id,
                    provider_id=provider_id,
                    agent_id=self.agent_id,
                    retry_task_id=logical_task_id,
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Operator provider retry authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX):
            repair_store = ProviderContractRepairAuthorizationStore.from_reservation_root(
                _provider_call_reservations_dir(self.provider)
            )
            try:
                repair_authorization = repair_store.require_pending_repair(
                    workflow_id=message.workflow_id,
                    provider_id=provider_id,
                    agent_id=self.agent_id,
                    repair_task_id=logical_task_id,
                    repair_model_id=effective_model,
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Provider contract repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(PROVIDER_CAPABILITY_REPAIR_SUFFIX):
            capability_store = (
                ProviderCapabilityRepairAuthorizationStore.from_reservation_root(
                    _provider_call_reservations_dir(self.provider)
                )
            )
            try:
                capability_authorization = capability_store.require_pending_repair(
                    workflow_id=message.workflow_id,
                    provider_id=provider_id,
                    agent_id=self.agent_id,
                    repair_task_id=logical_task_id,
                    repair_model_id=effective_model,
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Provider capability repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(PROVIDER_OUTPUT_REPAIR_SUFFIX):
            output_repair_store = (
                ProviderOutputRepairAuthorizationStore.from_reservation_root(
                    _provider_call_reservations_dir(self.provider)
                )
            )
            try:
                output_repair_authorization = (
                    output_repair_store.require_pending_repair(
                        workflow_id=message.workflow_id,
                        provider_id=provider_id,
                        agent_id=self.agent_id,
                        repair_task_id=logical_task_id,
                        model_id=effective_model,
                    )
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Provider output repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(RUNTIME_MODEL_REPAIR_SUFFIX):
            runtime_model_repair_store = (
                RuntimeModelRepairAuthorizationStore.from_reservation_root(
                    _provider_call_reservations_dir(self.provider)
                )
            )
            try:
                runtime_model_repair_authorization = (
                    runtime_model_repair_store.require_pending_repair(
                        workflow_id=message.workflow_id,
                        provider_id=provider_id,
                        agent_id=self.agent_id,
                        repair_task_id=logical_task_id,
                        runtime_model_id=effective_model,
                    )
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Runtime model repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(RUNTIME_MODEL_OUTPUT_REPAIR_SUFFIX):
            runtime_output_repair_store = (
                RuntimeModelOutputRepairAuthorizationStore.from_reservation_root(
                    _provider_call_reservations_dir(self.provider)
                )
            )
            try:
                runtime_output_repair_authorization = (
                    runtime_output_repair_store.require_pending_repair(
                        workflow_id=message.workflow_id,
                        provider_id=provider_id,
                        agent_id=self.agent_id,
                        repair_task_id=logical_task_id,
                        model_id=effective_model,
                    )
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Runtime output repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(RUNTIME_ADAPTER_REPAIR_SUFFIX):
            runtime_adapter_repair_store = (
                RuntimeAdapterRepairAuthorizationStore.from_reservation_root(
                    _provider_call_reservations_dir(self.provider)
                )
            )
            try:
                runtime_adapter_repair_authorization = (
                    runtime_adapter_repair_store.require_pending_repair(
                        workflow_id=message.workflow_id,
                        provider_id=provider_id,
                        agent_id=self.agent_id,
                        repair_task_id=logical_task_id,
                        model_id=effective_model,
                    )
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Runtime adapter repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(RUNTIME_IDENTITY_HYDRATION_REPAIR_SUFFIX):
            runtime_identity_repair_store = (
                RuntimeIdentityHydrationRepairAuthorizationStore.from_reservation_root(
                    _provider_call_reservations_dir(self.provider)
                )
            )
            try:
                runtime_identity_repair_authorization = (
                    runtime_identity_repair_store.require_pending_repair(
                        workflow_id=message.workflow_id,
                        provider_id=provider_id,
                        agent_id=self.agent_id,
                        repair_task_id=logical_task_id,
                        model_id=effective_model,
                    )
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Runtime identity repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc
        elif logical_task_id.endswith(RUNTIME_PROVENANCE_HYDRATION_REPAIR_SUFFIX):
            runtime_provenance_repair_store = (
                RuntimeProvenanceHydrationRepairAuthorizationStore.from_reservation_root(
                    _provider_call_reservations_dir(self.provider)
                )
            )
            try:
                runtime_provenance_repair_authorization = (
                    runtime_provenance_repair_store.require_pending_repair(
                        workflow_id=message.workflow_id,
                        provider_id=provider_id,
                        agent_id=self.agent_id,
                        repair_task_id=logical_task_id,
                        model_id=effective_model,
                    )
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    f"Runtime provenance repair authorization rejected for {self.agent_id} "
                    f"task {logical_task_id}: {exc}",
                    provider=provider_name,
                    model_id=effective_model,
                ) from exc

        try:
            reservation_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NonRetryableAgentError(
                f"Provider invocation could not persist a reservation for {self.agent_id} "
                f"task {logical_task_id}; provider call blocked",
                provider=provider_name,
                model_id=effective_model,
            ) from exc

        try:
            with reservation_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(reservation, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if retry_authorization is not None and retry_store is not None:
                retry_store.consume(
                    retry_authorization,
                    reservation_path=reservation_path,
                )
            if repair_authorization is not None and repair_store is not None:
                repair_store.consume(
                    repair_authorization,
                    reservation_path=reservation_path,
                )
            if (
                capability_authorization is not None
                and capability_store is not None
            ):
                capability_store.consume(
                    capability_authorization,
                    reservation_path=reservation_path,
                )
            if (
                output_repair_authorization is not None
                and output_repair_store is not None
            ):
                output_repair_store.consume(
                    output_repair_authorization,
                    reservation_path=reservation_path,
                )
            if (
                runtime_model_repair_authorization is not None
                and runtime_model_repair_store is not None
            ):
                runtime_model_repair_store.consume(
                    runtime_model_repair_authorization,
                    reservation_path=reservation_path,
                )
            if (
                runtime_output_repair_authorization is not None
                and runtime_output_repair_store is not None
            ):
                runtime_output_repair_store.consume(
                    runtime_output_repair_authorization,
                    reservation_path=reservation_path,
                )
            if (
                runtime_adapter_repair_authorization is not None
                and runtime_adapter_repair_store is not None
            ):
                runtime_adapter_repair_store.consume(
                    runtime_adapter_repair_authorization,
                    reservation_path=reservation_path,
                )
            if (
                runtime_identity_repair_authorization is not None
                and runtime_identity_repair_store is not None
            ):
                runtime_identity_repair_store.consume(
                    runtime_identity_repair_authorization,
                    reservation_path=reservation_path,
                )
            if (
                runtime_provenance_repair_authorization is not None
                and runtime_provenance_repair_store is not None
            ):
                runtime_provenance_repair_store.consume(
                    runtime_provenance_repair_authorization,
                    reservation_path=reservation_path,
                )
        except FileExistsError as exc:
            raise NonRetryableAgentError(
                f"Persistent reservation blocked a repeated call for {self.agent_id} "
                f"task {logical_task_id}",
                provider=provider_name,
                model_id=effective_model,
            ) from exc

        except OSError as exc:
            raise NonRetryableAgentError(
                f"Provider invocation could not persist a reservation for {self.agent_id} "
                f"task {logical_task_id}; provider call blocked",
                provider=provider_name,
                model_id=effective_model,
            ) from exc

    def _logical_task_id(
        self,
        message: PMPMessage,
        *,
        input_data: dict | None = None,
    ) -> str:
        return self._explicit_logical_task_id(
            message,
            input_data=input_data,
        ) or self.agent_id

    def _explicit_logical_task_id(
        self,
        message: PMPMessage,
        *,
        input_data: dict | None = None,
    ) -> str | None:
        override = message.metadata.extensions.get("provider_task_id")
        if override is not None:
            if not isinstance(override, str) or not override.strip():
                raise NonRetryableAgentError(
                    f"Invalid provider_task_id override for {self.agent_id}",
                    provider=type(self.provider).__name__,
                    model_id=self.model,
                    automatic_retry_allowed=False,
                )
            return override
        task_id = (input_data or {}).get("task_id") or message.payload.get("task_id")
        return str(task_id) if task_id else None

    @staticmethod
    def _reservation_path_component(value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
            return value
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"id-{digest}"

    def resolve_result_message_type(self, request: PMPMessage) -> MessageType:
        return self.output_message_type

    @property
    def agent_prompt(self) -> str:
        name = self.agent_id.split(".", 1)[1]
        path = Path(BASE_DIR) / self.prompt_layer / "prompts" / f"{name}.md"
        return path.read_text(encoding="utf-8")

    @property
    def system_prompt(self) -> str:
        """Compatibility alias for code that inspected the v1 fixed prompt."""

        return self.agent_prompt

    def create_result_message(
        self,
        request: PMPMessage,
        result: BaseModel,
        retry_count: int,
        *,
        result_message_type: MessageType | None = None,
        snapshot: RoleDefinitionSnapshot | None = None,
    ) -> PMPMessage:
        previous_stage = (
            request.context.previous_stage
            if self.use_request_previous_stage
            else request.context.current_stage
        )
        return PMPMessage.create(
            workflow_id=request.workflow_id,
            parent_message_id=request.message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=request.sender_agent_id,
            message_type=result_message_type or self.resolve_result_message_type(request),
            objective=f"{self.agent_id} {self.result_objective_suffix}",
            payload=result.model_dump(mode="json"),
            constraints=request.constraints,
            context=PMPContext(
                current_stage=self.agent_id,
                previous_stage=previous_stage,
                next_stage=self.manager_agent_id,
            ),
            metadata=PMPMetadata(
                status=MessageStatus.COMPLETED,
                retry_count=retry_count,
                extensions=role_definition_extensions(snapshot),
            ),
        )

    def create_error_message(
        self,
        request: PMPMessage,
        exc: Exception,
        *,
        snapshot: RoleDefinitionSnapshot | None = None,
        model_id: str | None = None,
    ) -> PMPMessage:
        retry_count = max(int(getattr(exc, "retry_count", 0)), 0)
        root_exception = self._root_exception(exc)
        task_id = self._logical_task_id(request)
        payload = {
            "error_code": getattr(exc, "error_code", type(exc).__name__),
            "message": self._safe_error_text(str(exc)),
            "workflow_id": request.workflow_id,
            "task_id": task_id,
            "agent_id": self.agent_id,
            "model_id": getattr(exc, "model_id", None) or model_id or self.model,
            "provider": getattr(exc, "provider", None) or type(self.provider).__name__,
            "error_class": type(exc).__name__,
            "http_status": getattr(exc, "http_status", None),
            "retry_count": retry_count,
            "automatic_retry_allowed": bool(
                getattr(exc, "automatic_retry_allowed", True)
            ),
            "root_exception_type": type(root_exception).__name__,
            "root_exception_message": self._safe_error_text(str(root_exception)),
            "validation_field_path": self._validation_field_path(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for field in ("requested_action", "violated_rule"):
            value = getattr(exc, field, None)
            if value:
                payload[field] = value
        for field in (
            "response_content_sha256",
            "response_content_length",
            "response_root_type",
            "response_invalid_path",
        ):
            value = getattr(exc, field, None)
            if value is not None:
                payload[field] = value
        invalid_payload = getattr(exc, "invalid_payload", None)
        if isinstance(invalid_payload, dict):
            payload["invalid_payload"] = invalid_payload
            payload["validation_errors"] = getattr(exc, "validation_errors", [])
        if snapshot:
            payload.update(snapshot.trace())
        return PMPMessage.create(
            workflow_id=request.workflow_id,
            parent_message_id=request.message_id,
            sender_agent_id=self.agent_id,
            receiver_agent_id=request.sender_agent_id,
            message_type=MessageType.ERROR,
            objective=f"{self.agent_id} execution error",
            payload=payload,
            context=PMPContext(
                current_stage=self.agent_id,
                next_stage=self.error_next_stage or self.manager_agent_id,
            ),
            metadata=PMPMetadata(
                status=MessageStatus.FAILED,
                retry_count=retry_count,
                extensions=role_definition_extensions(snapshot),
            ),
        )

    @staticmethod
    def _root_exception(exc: Exception) -> Exception:
        root = exc
        visited: set[int] = set()
        while id(root) not in visited:
            visited.add(id(root))
            next_error = root.__cause__ or root.__context__
            if next_error is None:
                break
            root = next_error
        return root

    @staticmethod
    def _safe_error_text(message: str) -> str:
        redacted = re.sub(
            r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+",
            r"\1<redacted>",
            message,
        )
        redacted = re.sub(
            r"(?i)(OPENROUTER_API_KEY|DISCORD_BOT_TOKEN)(\s*[:=]\s*)\S+",
            r"\1\2<redacted>",
            redacted,
        )
        return re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]+", "Bearer <redacted>", redacted)

    @staticmethod
    def _validation_field_path(exc: Exception) -> str | None:
        if not isinstance(exc, PayloadValidationError):
            return None
        lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
        for line in lines[1:]:
            if not line.startswith(("Input should", "For further information")):
                return line
        return None
