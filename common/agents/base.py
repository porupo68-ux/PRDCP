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
from common.role_definitions import RoleDefinitionLoader
from common.role_definitions.agent_runtime import (
    prepare_agent_execution,
    role_definition_extensions,
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
    ) -> None:
        self.provider = provider
        self.payload_validator = payload_validator
        self.pmp_validator = pmp_validator
        self.model = model
        # Kept only for compatibility with early prototypes. RD runtime_config is
        # authoritative, so callers cannot silently override the role contract.
        self.legacy_max_technical_retries = max_technical_retries
        self.demo_safe_mode = demo_safe_mode
        self.rd_loader = rd_loader or RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=BASE_DIR / "storage" / "data" / "logs" / "rd_access.jsonl",
        )

    async def execute(self, message: PMPMessage) -> PMPMessage:
        snapshot: RoleDefinitionSnapshot | None = None
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
            if self.demo_safe_mode:
                self._reserve_demo_invocation(validated, payload)
            result, retry_count = await self.run(
                payload,
                system_prompt=execution.system_prompt,
                max_technical_retries=execution.runtime_config.technical_retry_limit,
                timeout_seconds=execution.runtime_config.timeout_seconds,
            )
            return self.create_result_message(
                validated,
                result,
                retry_count,
                result_message_type=result_message_type,
                snapshot=snapshot,
            )
        except Exception as exc:
            return self.create_error_message(message, exc, snapshot=snapshot)

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
    ) -> tuple[BaseModel, int]:
        input_data = payload.model_dump(mode="json")
        effective_retry_limit = (
            0 if self.demo_safe_mode else min(max(max_technical_retries, 0), 1)
        )
        for attempt in range(effective_retry_limit + 1):
            try:
                raw = await asyncio.wait_for(
                    self.provider.generate_structured(
                        model=self.model,
                        system_prompt=system_prompt,
                        input_data=input_data,
                        output_schema=self.output_schema,
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=timeout_seconds,
                )
                return self.payload_validator.validate(raw, self.output_schema), attempt
            except PayloadValidationError as exc:
                exc.retry_count = attempt
                raise
            except NonRetryableAgentError as exc:
                exc.retry_count = attempt
                raise
            except RetryableAgentError as exc:
                exc.retry_count = attempt
                if attempt >= effective_retry_limit:
                    raise
            except TimeoutError as exc:
                timeout_error = RetryableAgentError(
                    f"{self.agent_id} provider call timed out after {timeout_seconds} seconds",
                    retry_count=attempt,
                    provider=type(self.provider).__name__,
                    model_id=self.model,
                )
                if attempt >= effective_retry_limit:
                    raise timeout_error from exc
            except AgentExecutionError as exc:
                raise NonRetryableAgentError(
                    f"{self.agent_id} stopped on an unclassified provider error: {exc}",
                    http_status=exc.http_status,
                    retry_count=attempt,
                    provider=exc.provider or type(self.provider).__name__,
                    model_id=exc.model_id or self.model,
                ) from exc
        raise RuntimeError("unreachable retry state")

    def _reserve_demo_invocation(self, message: PMPMessage, payload: BaseModel) -> None:
        input_data = payload.model_dump(mode="json")
        task_id = input_data.get("task_id") or message.payload.get("task_id")
        logical_task_id = str(task_id or self.agent_id)
        provider_name = type(self.provider).__name__
        provider_id = getattr(self.provider, "provider_id", None)
        if not isinstance(provider_id, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9_-]{0,63}", provider_id
        ):
            raise NonRetryableAgentError(
                f"Demo Safe Mode requires a stable logical provider ID for {self.agent_id}; "
                "provider call blocked",
                provider=provider_name,
                model_id=self.model,
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
            "model_id": self.model,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            reservation_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NonRetryableAgentError(
                f"Demo Safe Mode could not persist a reservation for {self.agent_id} "
                f"task {logical_task_id}; provider call blocked",
                provider=provider_name,
                model_id=self.model,
            ) from exc

        try:
            with reservation_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(reservation, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise NonRetryableAgentError(
                f"Demo Safe Mode blocked a repeated call for {self.agent_id} "
                f"task {logical_task_id}",
                provider=provider_name,
                model_id=self.model,
            ) from exc
        except OSError as exc:
            raise NonRetryableAgentError(
                f"Demo Safe Mode could not persist a reservation for {self.agent_id} "
                f"task {logical_task_id}; provider call blocked",
                provider=provider_name,
                model_id=self.model,
            ) from exc

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
    ) -> PMPMessage:
        retry_count = max(int(getattr(exc, "retry_count", 0)), 0)
        root_exception = self._root_exception(exc)
        task_id = request.payload.get("task_id") if isinstance(request.payload, dict) else None
        payload = {
            "error_code": getattr(exc, "error_code", type(exc).__name__),
            "message": self._safe_error_text(str(exc)),
            "workflow_id": request.workflow_id,
            "task_id": task_id,
            "agent_id": self.agent_id,
            "model_id": getattr(exc, "model_id", None) or self.model,
            "provider": getattr(exc, "provider", None) or type(self.provider).__name__,
            "error_class": type(exc).__name__,
            "http_status": getattr(exc, "http_status", None),
            "retry_count": retry_count,
            "root_exception_type": type(root_exception).__name__,
            "root_exception_message": self._safe_error_text(str(root_exception)),
            "validation_field_path": self._validation_field_path(exc),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for field in ("requested_action", "violated_rule"):
            value = getattr(exc, field, None)
            if value:
                payload[field] = value
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
