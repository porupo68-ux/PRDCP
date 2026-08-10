from __future__ import annotations

import asyncio
from abc import ABC
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from pydantic import BaseModel

from common.models.errors import AgentExecutionError, PayloadValidationError
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
    ) -> None:
        self.provider = provider
        self.payload_validator = payload_validator
        self.pmp_validator = pmp_validator
        self.model = model
        # Kept only for compatibility with early prototypes. RD runtime_config is
        # authoritative, so callers cannot silently override the role contract.
        self.legacy_max_technical_retries = max_technical_retries
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
        last_error: Exception | None = None
        input_data = payload.model_dump(mode="json")
        for attempt in range(max_technical_retries + 1):
            try:
                raw = await asyncio.wait_for(
                    self.provider.generate_structured(
                        model=self.model,
                        system_prompt=system_prompt,
                        input_data=input_data,
                        output_schema=self.output_schema,
                    ),
                    timeout=timeout_seconds,
                )
                return self.payload_validator.validate(raw, self.output_schema), attempt
            except (AgentExecutionError, PayloadValidationError, TimeoutError) as exc:
                last_error = exc
        raise AgentExecutionError(
            f"{self.agent_id} exceeded technical retry limit: {last_error}"
        ) from last_error

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
        payload = {
            "error_code": getattr(exc, "error_code", type(exc).__name__),
            "message": str(exc),
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
                extensions=role_definition_extensions(snapshot),
            ),
        )
