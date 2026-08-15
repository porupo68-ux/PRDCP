from __future__ import annotations

import asyncio
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
        self.models = models or {}
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
    ) -> OutputT:
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
            output_schema=strict_output_schema(output_schema),
        )
        self.rd_loader.access_log.record(
            "role_context_built",
            agent_id="deliberation.manager",
            role_definition_version=snapshot.role_definition_version,
            role_definition_hash=snapshot.content_hash,
        )
        invocation_key = (workflow_id, stage)
        if self.demo_safe_mode:
            if invocation_key in self._manager_invocations and not recovery:
                raise NonRetryableAgentError(
                    f"Demo Safe Mode blocked a repeated deliberation.manager call for {stage}",
                    provider=type(self.provider).__name__,
                    model_id=self.models.get("deliberation.manager") or "mock",
                )
            self._manager_invocations.add(invocation_key)

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
                        model=self.models.get("deliberation.manager") or "mock",
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
                if attempt >= retry_limit:
                    raise
            except TimeoutError as exc:
                timeout_error = RetryableAgentError(
                    f"deliberation.manager provider call timed out during {stage}",
                    retry_count=attempt,
                    provider=type(self.provider).__name__,
                    model_id=self.models.get("deliberation.manager") or "mock",
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
                    model_id=exc.model_id or self.models.get("deliberation.manager") or "mock",
                ) from exc
        raise RuntimeError("unreachable retry state")

    @property
    def agent_ids(self) -> set[str]:
        return set(self._agents)
