from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from common.models.errors import (
    AgentExecutionError,
    NonRetryableAgentError,
    PayloadValidationError,
    RetryableAgentError,
)
from common.prompting import PRDCP_COMMON_RULES, PromptBuilder
from common.provider_retry import OPERATOR_RETRY_SUFFIX, ProviderRetryAuthorizationStore
from common.provider_contract_repair import (
    PROVIDER_CONTRACT_REPAIR_SUFFIX,
    ProviderContractRepairAuthorizationStore,
)
from common.role_definitions import RoleDefinitionExtractor, RoleDefinitionLoader
from common.structured_outputs import strict_output_schema
from common.validation import PMPValidator, PayloadValidator
from config.settings import BASE_DIR
from deliberation.agents import (
    ArgumentAnalyst,
    CausalStructuralAnalyst,
    CounterargumentAnalyst,
    DeliberationQualityReviewer,
    StakeholderResponseAnalyst,
)
from providers.base import ModelProvider


OutputT = TypeVar("OutputT", bound=BaseModel)


class DeliberationRegistry:
    def __init__(
        self,
        provider: ModelProvider,
        models: dict[str, str] | None = None,
        *,
        rd_loader: RoleDefinitionLoader | None = None,
        demo_safe_mode: bool = True,
    ) -> None:
        payload_validator = PayloadValidator()
        pmp_validator = PMPValidator()
        self.provider = provider
        self.payload_validator = payload_validator
        self.models = dict(models or {})
        self.demo_safe_mode = demo_safe_mode
        self._manager_invocations: set[tuple[str, str]] = set()
        self.rd_loader = rd_loader or RoleDefinitionLoader.from_project(
            BASE_DIR,
            access_log_path=Path(BASE_DIR) / "storage" / "data" / "logs" / "rd_access.jsonl",
        )
        agent_types = [
            ArgumentAnalyst,
            CausalStructuralAnalyst,
            StakeholderResponseAnalyst,
            CounterargumentAnalyst,
            DeliberationQualityReviewer,
        ]
        self._agents = {
            agent_type.agent_id: agent_type(
                provider,
                payload_validator,
                pmp_validator,
                model=self.models.get(agent_type.agent_id) or "mock",
                rd_loader=self.rd_loader,
                demo_safe_mode=demo_safe_mode,
            )
            for agent_type in agent_types
        }

    def get(self, agent_id: str):
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise KeyError(f"Deliberation agent is not registered: {agent_id}") from exc

    async def integrate(
        self,
        *,
        input_data: dict,
        output_schema: type[OutputT],
        stage: str,
        workflow_id: str,
        max_technical_retries: int = 2,
        recovery: bool = False,
        logical_task_id: str | None = None,
        model_override: str | None = None,
    ) -> OutputT:
        effective_model = model_override or self.models.get("deliberation.manager") or "mock"
        agent_prompt = (Path(BASE_DIR) / "deliberation" / "prompts" / "manager.md").read_text(
            encoding="utf-8"
        )
        snapshot = self.rd_loader.load("deliberation.manager")
        extractor = RoleDefinitionExtractor()
        runtime_config = extractor.extract_runtime_config(snapshot)
        prompt = PromptBuilder().build(
            common_rules=PRDCP_COMMON_RULES,
            role_context=extractor.extract_llm_context(snapshot),
            agent_prompt=agent_prompt + f"\nCurrent integration stage: {stage}",
            task_constraints={"integration_stage": stage},
            output_schema=strict_output_schema(
                output_schema,
                input_data=input_data,
            ),
        )
        self.rd_loader.access_log.record(
            "role_context_built",
            agent_id="deliberation.manager",
            role_definition_version=snapshot.role_definition_version,
            role_definition_hash=snapshot.content_hash,
        )
        preflight = getattr(self.provider, "validate_request_budget", None)
        if callable(preflight):
            preflight(
                agent_id="deliberation.manager",
                model=effective_model,
                system_prompt=prompt,
                input_data=input_data,
                output_schema=output_schema,
            )
        invocation_key = (workflow_id, stage)
        if self.demo_safe_mode:
            if invocation_key in self._manager_invocations and not recovery:
                raise NonRetryableAgentError(
                    f"Demo Safe Mode blocked a repeated deliberation.manager call for {stage}",
                    provider=type(self.provider).__name__,
                    model_id=effective_model,
                )
            self._manager_invocations.add(invocation_key)
        self._reserve_manager_invocation(
            workflow_id=workflow_id,
            logical_task_id=logical_task_id or f"deliberation_manager_{stage}",
            stage=stage,
            model_id=effective_model,
        )

        configured_retry_limit = min(
            max(max_technical_retries, 0),
            runtime_config.technical_retry_limit,
            1,
        )
        retry_limit = 0 if self.demo_safe_mode else configured_retry_limit
        for attempt in range(retry_limit + 1):
            try:
                raw = await asyncio.wait_for(
                    self.provider.generate_structured(
                        model=effective_model,
                        system_prompt=prompt,
                        input_data=input_data,
                        output_schema=output_schema,
                        timeout_seconds=runtime_config.timeout_seconds,
                    ),
                    timeout=runtime_config.timeout_seconds,
                )
                return self.payload_validator.validate(raw, output_schema)
            except PayloadValidationError as exc:
                exc.retry_count = attempt
                raise
            except NonRetryableAgentError as exc:
                exc.retry_count = attempt
                raise
            except RetryableAgentError as exc:
                exc.retry_count = attempt
                if not exc.automatic_retry_allowed or attempt >= retry_limit:
                    raise
            except TimeoutError as exc:
                timeout_error = RetryableAgentError(
                    f"deliberation.manager provider call timed out during {stage}",
                    retry_count=attempt,
                    provider=type(self.provider).__name__,
                    model_id=effective_model,
                )
                if attempt >= retry_limit:
                    raise timeout_error from exc
            except AgentExecutionError as exc:
                raise NonRetryableAgentError(
                    f"deliberation.manager stopped on an unclassified provider error "
                    f"during {stage}: {exc}",
                    http_status=exc.http_status,
                    retry_count=attempt,
                    provider=exc.provider or type(self.provider).__name__,
                    model_id=exc.model_id or effective_model,
                ) from exc
        raise RuntimeError("unreachable retry state")

    def _reserve_manager_invocation(
        self,
        *,
        workflow_id: str,
        logical_task_id: str,
        stage: str,
        model_id: str,
    ) -> None:
        provider_id = getattr(self.provider, "provider_id", None)
        if not isinstance(provider_id, str):
            if self.demo_safe_mode:
                raise NonRetryableAgentError(
                    "Deliberation Manager requires a stable provider ID before invocation",
                    provider=type(self.provider).__name__,
                    model_id=model_id,
                )
            return
        configured_root = getattr(self.provider, "reservation_root", None)
        if configured_root is None:
            configured_data = os.getenv("PRDCP_DATA_DIR", "").strip()
            data_dir = Path(configured_data) if configured_data else Path(BASE_DIR) / "storage" / "data"
            reservation_root = data_dir / "provider_call_reservations"
        else:
            reservation_root = Path(configured_root)
        store = ProviderRetryAuthorizationStore.from_reservation_root(reservation_root)
        reservation_path = store.reservation_path(
            provider_id=provider_id,
            workflow_id=workflow_id,
            task_id=logical_task_id,
        )
        reservation = {
            "workflow_id": workflow_id,
            "task_id": logical_task_id,
            "agent_id": "deliberation.manager",
            "stage": stage,
            "provider": type(self.provider).__name__,
            "model_id": model_id,
            "reserved_at": datetime.now(timezone.utc).isoformat(),
        }
        retry_authorization = None
        repair_authorization = None
        repair_store = None
        if logical_task_id.endswith(OPERATOR_RETRY_SUFFIX):
            try:
                retry_authorization = store.require_pending_retry(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    agent_id="deliberation.manager",
                    retry_task_id=logical_task_id,
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    "Operator provider retry authorization rejected for "
                    f"deliberation.manager task {logical_task_id}: {exc}",
                    provider=type(self.provider).__name__,
                    model_id=model_id,
                ) from exc
        elif logical_task_id.endswith(PROVIDER_CONTRACT_REPAIR_SUFFIX):
            repair_store = ProviderContractRepairAuthorizationStore.from_reservation_root(
                reservation_root
            )
            try:
                repair_authorization = repair_store.require_pending_repair(
                    workflow_id=workflow_id,
                    provider_id=provider_id,
                    agent_id="deliberation.manager",
                    repair_task_id=logical_task_id,
                    repair_model_id=model_id,
                )
            except ValueError as exc:
                raise NonRetryableAgentError(
                    "Provider contract repair authorization rejected for "
                    f"deliberation.manager task {logical_task_id}: {exc}",
                    provider=type(self.provider).__name__,
                    model_id=model_id,
                ) from exc
        try:
            reservation_path.parent.mkdir(parents=True, exist_ok=True)
            with reservation_path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(reservation, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if retry_authorization is not None:
                store.consume(
                    retry_authorization,
                    reservation_path=reservation_path,
                )
            if repair_authorization is not None and repair_store is not None:
                repair_store.consume(
                    repair_authorization,
                    reservation_path=reservation_path,
                )
        except FileExistsError as exc:
            raise NonRetryableAgentError(
                "Persistent reservation blocked a repeated deliberation.manager call "
                f"for {logical_task_id}",
                provider=type(self.provider).__name__,
                model_id=model_id,
            ) from exc
        except OSError as exc:
            raise NonRetryableAgentError(
                "Could not persist deliberation.manager provider reservation for "
                f"{logical_task_id}",
                provider=type(self.provider).__name__,
                model_id=model_id,
            ) from exc

    @property
    def agent_ids(self) -> set[str]:
        return set(self._agents)
